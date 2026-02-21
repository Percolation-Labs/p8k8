import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import { KubectlV31Layer } from '@aws-cdk/lambda-layer-kubectl-v31';
import { KubectlV33Layer } from '@aws-cdk/lambda-layer-kubectl-v33';
import { Construct } from 'constructs';
import { ClusterConfig, getAddonVersions } from './config';

export interface ManagementClusterStackProps extends cdk.StackProps {
  ecrRepository: ecr.Repository;
  natGateways?: number; // Number of NAT gateways (1 for testing, 3 for HA production)
  config: ClusterConfig;
}

export class ManagementClusterStack extends cdk.Stack {
  public readonly cluster: eks.Cluster;

  constructor(scope: Construct, id: string, props: ManagementClusterStackProps) {
    super(scope, id, props);

    // VPC for the management cluster
    // Use 1 NAT gateway for testing (saves cost + EIPs), 3 for production HA
    const natGateways = props.natGateways ?? 1;
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 3,
      natGateways, // Configurable: 1 for testing, 3 for HA
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 24,
          name: 'Private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
      ],
    });

    // Tag subnets for AWS Load Balancer Controller
    vpc.publicSubnets.forEach((subnet) => {
      cdk.Tags.of(subnet).add('kubernetes.io/role/elb', '1');
    });
    vpc.privateSubnets.forEach((subnet) => {
      cdk.Tags.of(subnet).add('kubernetes.io/role/internal-elb', '1');
    });

    // Determine Kubernetes version and kubectl layer
    const k8sVersion = props.config.kubernetesVersion;
    const k8sVersionEnum = (eks.KubernetesVersion as any)[`V${k8sVersion.replace('.', '_')}`];
    const kubectlLayer = k8sVersion === '1.33'
      ? new KubectlV33Layer(this, 'KubectlLayer')
      : new KubectlV31Layer(this, 'KubectlLayer');

    // Get compatible addon versions for this K8s version
    const addonVersions = getAddonVersions(k8sVersion);

    // EKS Cluster with Pod Identity
    this.cluster = new eks.Cluster(this, 'Cluster', {
      clusterName: `${props.config.clusterNamePrefix}-management`,
      version: k8sVersionEnum,
      vpc,
      vpcSubnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],

      // Disable default capacity (we'll add managed node group separately)
      defaultCapacity: 0,

      // Enable Pod Identity (modern alternative to IRSA)
      authenticationMode: eks.AuthenticationMode.API_AND_CONFIG_MAP,

      // kubectl layer for managing the cluster
      kubectlLayer: kubectlLayer,
    });

    // Grant admin access to the configured IAM role/user (enables kubectl access)
    // This allows the deploying user to access the cluster with kubectl
    // Set ADMIN_ROLE_ARN env var to your IAM user/role ARN
    if (props.config.adminRoleArn) {
      new eks.CfnAccessEntry(this, 'AdminAccessEntry', {
        clusterName: this.cluster.clusterName,
        principalArn: props.config.adminRoleArn,
        type: 'STANDARD',
        accessPolicies: [{
          policyArn: 'arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy',
          accessScope: {
            type: 'cluster',
          },
        }],
      });
    }

    // Create managed node group for management workloads
    this.cluster.addNodegroupCapacity('ManagementNodeGroup', {
      instanceTypes: [new ec2.InstanceType('t3.small')],
      minSize: 2,
      maxSize: 3,
      desiredSize: 2,
      amiType: eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
      diskSize: 20,
      capacityType: eks.CapacityType.ON_DEMAND, // Stable for management
    });

    // Install core EKS addons (VPC-CNI, CoreDNS, KubeProxy, EBS-CSI)
    // Versions are automatically selected based on K8s version
    new eks.CfnAddon(this, 'VpcCniAddon', {
      clusterName: this.cluster.clusterName,
      addonName: 'vpc-cni',
      addonVersion: addonVersions.vpcCni,
      resolveConflicts: 'OVERWRITE',
    });

    new eks.CfnAddon(this, 'CoreDnsAddon', {
      clusterName: this.cluster.clusterName,
      addonName: 'coredns',
      addonVersion: addonVersions.coreDns,
      resolveConflicts: 'OVERWRITE',
    });

    new eks.CfnAddon(this, 'KubeProxyAddon', {
      clusterName: this.cluster.clusterName,
      addonName: 'kube-proxy',
      addonVersion: addonVersions.kubeProxy,
      resolveConflicts: 'OVERWRITE',
    });

    // EBS CSI Driver requires IAM role with Pod Identity trust policy
    // According to AWS docs, Pod Identity requires BOTH sts:AssumeRole AND sts:TagSession
    const ebsCsiRole = new iam.Role(this, 'EbsCsiRole', {
      roleName: `${props.config.clusterNamePrefix}-management-ebs-csi`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonEBSCSIDriverPolicy'),
      ],
    });

    // Manually add the complete trust policy with sts:TagSession
    ebsCsiRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('pods.eks.amazonaws.com')],
        actions: ['sts:AssumeRole', 'sts:TagSession'],
        conditions: {
          'StringEquals': {
            'aws:SourceAccount': this.account,
          },
          'ArnEquals': {
            'aws:SourceArn': `arn:aws:eks:${this.region}:${this.account}:cluster/${this.cluster.clusterName}`,
          },
        },
      })
    );

    // Create pod identity association for EBS CSI driver
    new eks.CfnPodIdentityAssociation(this, 'EbsCsiPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'kube-system',
      serviceAccount: 'ebs-csi-controller-sa',
      roleArn: ebsCsiRole.roleArn,
    });

    new eks.CfnAddon(this, 'EbsCsiAddon', {
      clusterName: this.cluster.clusterName,
      addonName: 'aws-ebs-csi-driver',
      addonVersion: addonVersions.ebsCsi,
      resolveConflicts: 'OVERWRITE',
      serviceAccountRoleArn: ebsCsiRole.roleArn,
    });

    // Install EKS Pod Identity Agent addon
    // Required for Pod Identity authentication (modern alternative to IRSA)
    new eks.CfnAddon(this, 'PodIdentityAgentAddon', {
      clusterName: this.cluster.clusterName,
      addonName: 'eks-pod-identity-agent',
      addonVersion: addonVersions.podIdentityAgent,
      resolveConflicts: 'OVERWRITE',
    });

    // Create IAM role for External Secrets to access Secrets Manager
    const externalSecretsRole = new iam.Role(this, 'ExternalSecretsRole', {
      roleName: `${props.config.clusterNamePrefix}-management-external-secrets`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      description: 'Pod identity role for External Secrets Operator',
    });

    externalSecretsRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('pods.eks.amazonaws.com')],
        actions: ['sts:AssumeRole', 'sts:TagSession'],
        conditions: {
          'StringEquals': {
            'aws:SourceAccount': this.account,
          },
          'ArnEquals': {
            'aws:SourceArn': `arn:aws:eks:${this.region}:${this.account}:cluster/${this.cluster.clusterName}`,
          },
        },
      })
    );

    externalSecretsRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'secretsmanager:GetSecretValue',
          'secretsmanager:DescribeSecret',
          'secretsmanager:ListSecrets',
        ],
        resources: ['*'], // Scope down in production to specific secret ARNs
      })
    );

    // Add Parameter Store permissions for External Secrets
    externalSecretsRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'ssm:GetParameter',
          'ssm:GetParameters',
          'ssm:GetParametersByPath',
        ],
        resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter/${props.config.clusterNamePrefix}/*`],
      })
    );

    // Create IAM role for AWS Load Balancer Controller
    const albControllerRole = new iam.Role(this, 'AlbControllerRole', {
      roleName: `${props.config.clusterNamePrefix}-management-alb-controller`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      description: 'Pod identity role for AWS Load Balancer Controller',
    });

    albControllerRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('pods.eks.amazonaws.com')],
        actions: ['sts:AssumeRole', 'sts:TagSession'],
        conditions: {
          'StringEquals': {
            'aws:SourceAccount': this.account,
          },
          'ArnEquals': {
            'aws:SourceArn': `arn:aws:eks:${this.region}:${this.account}:cluster/${this.cluster.clusterName}`,
          },
        },
      })
    );

    // ALB Controller IAM policy (simplified - full policy in worker cluster)
    albControllerRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'ec2:DescribeVpcs',
          'ec2:DescribeSubnets',
          'ec2:DescribeSecurityGroups',
          'ec2:DescribeInstances',
          'ec2:DescribeNetworkInterfaces',
          'ec2:DescribeTags',
          'ec2:CreateTags',
          'ec2:DeleteTags',
          'ec2:CreateSecurityGroup',
          'ec2:DeleteSecurityGroup',
          'ec2:AuthorizeSecurityGroupIngress',
          'ec2:RevokeSecurityGroupIngress',
          'elasticloadbalancing:*',
          'cognito-idp:DescribeUserPoolClient',
          'acm:ListCertificates',
          'acm:DescribeCertificate',
          'iam:CreateServiceLinkedRole',
          'iam:GetServerCertificate',
          'iam:ListServerCertificates',
          'waf-regional:*',
          'wafv2:*',
          'shield:*',
        ],
        resources: ['*'],
      })
    );

    // Create IAM role for ArgoCD to access ECR (OCI registry support)
    const argoCDECRRole = new iam.Role(this, 'ArgoCDECRRole', {
      roleName: `${props.config.clusterNamePrefix}-management-argocd-ecr`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      description: 'Pod identity role for ArgoCD to access ECR as OCI registry',
    });

    argoCDECRRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('pods.eks.amazonaws.com')],
        actions: ['sts:AssumeRole', 'sts:TagSession'],
        conditions: {
          'StringEquals': {
            'aws:SourceAccount': this.account,
          },
          'ArnEquals': {
            'aws:SourceArn': `arn:aws:eks:${this.region}:${this.account}:cluster/${this.cluster.clusterName}`,
          },
        },
      })
    );

    argoCDECRRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:GetAuthorizationToken',
          'ecr:BatchCheckLayerAvailability',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
          'ecr:DescribeRepositories',
          'ecr:ListImages',
          'ecr:DescribeImages',
        ],
        resources: ['*'], // Scope to specific ECR ARNs in production
      })
    );

    // Create pod identity association for ALB Controller
    const albControllerPodIdentity = new eks.CfnPodIdentityAssociation(this, 'ALBControllerPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'kube-system',
      serviceAccount: 'aws-load-balancer-controller',
      roleArn: albControllerRole.roleArn,
    });

    // Deploy AWS Load Balancer Controller via Helm FIRST
    // Other Helm charts depend on its webhook being ready
    const albController = this.cluster.addHelmChart('ALBController', {
      chart: 'aws-load-balancer-controller',
      repository: 'https://aws.github.io/eks-charts',
      namespace: 'kube-system',
      version: '1.14.0',
      values: {
        clusterName: this.cluster.clusterName,
        region: this.region,
        vpcId: vpc.vpcId,
        serviceAccount: {
          create: true,
          name: 'aws-load-balancer-controller',
          annotations: {},
        },
        enableShield: false,
        enableWaf: false,
        enableWafv2: false,
      },
    });

    albController.node.addDependency(albControllerPodIdentity);

    // Install ArgoCD via Helm (optional - controlled by ENABLE_ARGOCD env var)
    // Set ENABLE_ARGOCD=false or ARGOCD_VERSION=x.x.x to customize
    // If disabled, use manual kubectl installation: kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
    if (props.config.enableArgoCD) {
      const argocd = this.cluster.addHelmChart('ArgoCD', {
        chart: 'argo-cd',
        repository: 'https://argoproj.github.io/argo-helm',
        namespace: 'argocd',
        createNamespace: true,
        version: props.config.argoCDVersion,
        values: {
          server: {
            service: {
              type: 'LoadBalancer', // Expose ArgoCD UI via AWS NLB
            },
          },
          configs: {
            // Configure OCI registry repositories
            // ArgoCD can pull Helm charts from OCI registries (ECR, GHCR, Docker Hub, etc.)
            repositories: {
              // ECR OCI Helm repository (uses Pod Identity for auth)
              'ecr-oci': {
                name: 'ecr-oci',
                type: 'helm',
                url: `${this.account}.dkr.ecr.${this.region}.amazonaws.com`,
                enableOCI: 'true',
              },
              // GitHub Container Registry (public charts)
              'ghcr-oci': {
                name: 'ghcr-oci',
                type: 'helm',
                url: 'ghcr.io',
                enableOCI: 'true',
              },
            },
          },
        },
      });

      // Make ArgoCD wait for ALB Controller to be ready (avoids webhook timing issues)
      argocd.node.addDependency(albController);

      // Create pod identity association for ArgoCD ECR access
      const argoCDPodIdentity = new eks.CfnPodIdentityAssociation(this, 'ArgoCDPodIdentity', {
        clusterName: this.cluster.clusterName,
        namespace: 'argocd',
        serviceAccount: 'argocd-repo-server', // ArgoCD component that pulls OCI charts
        roleArn: argoCDECRRole.roleArn,
      });

      argoCDPodIdentity.node.addDependency(argocd);
    }

    // Outputs
    const prefix = props.config.clusterNamePrefix;

    new cdk.CfnOutput(this, 'ClusterName', {
      value: this.cluster.clusterName,
      description: 'Management cluster name',
      exportName: `${prefix}-management-cluster-name`,
    });

    new cdk.CfnOutput(this, 'ClusterArn', {
      value: this.cluster.clusterArn,
      description: 'Management cluster ARN',
      exportName: `${prefix}-management-cluster-arn`,
    });

    new cdk.CfnOutput(this, 'VpcId', {
      value: vpc.vpcId,
      description: 'VPC ID where cluster is deployed',
      exportName: `${prefix}-management-vpc-id`,
    });

    new cdk.CfnOutput(this, 'AlbControllerRoleArn', {
      value: albControllerRole.roleArn,
      description: 'IAM role ARN for AWS Load Balancer Controller',
      exportName: `${prefix}-management-alb-controller-role-arn`,
    });

    new cdk.CfnOutput(this, 'ExternalSecretsRoleArn', {
      value: externalSecretsRole.roleArn,
      description: 'IAM role ARN for External Secrets (use with kubectl/ArgoCD bootstrap)',
      exportName: `${prefix}-management-external-secrets-role-arn`,
    });

    new cdk.CfnOutput(this, 'ArgoCDECRRoleArn', {
      value: argoCDECRRole.roleArn,
      description: 'IAM role ARN for ArgoCD ECR access (use with kubectl bootstrap)',
      exportName: `${prefix}-management-argocd-ecr-role-arn`,
    });
  }
}
