import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as eks from 'aws-cdk-lib/aws-eks';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { KubectlV31Layer } from '@aws-cdk/lambda-layer-kubectl-v31';
import { KubectlV33Layer } from '@aws-cdk/lambda-layer-kubectl-v33';
import { Construct } from 'constructs';
import { ClusterConfig, getAddonVersions } from './config';

export interface WorkerClusterStackProps extends cdk.StackProps {
  clusterName: string;
  environment: string;
  ecrRepository: ecr.Repository;
  config: ClusterConfig;
}

export class WorkerClusterStack extends cdk.Stack {
  public readonly cluster: eks.Cluster;
  public readonly remAppRole: iam.Role;

  constructor(scope: Construct, id: string, props: WorkerClusterStackProps) {
    super(scope, id, props);

    // S3 bucket for application data (uploads, files, and general application storage)
    const appBucket = new s3.Bucket(this, 'AppBucket', {
      bucketName: `${props.config.clusterNamePrefix}-io-${props.environment}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true, // Always version for file history
      lifecycleRules: [
        {
          // Archive old versions to Glacier after 30 days
          noncurrentVersionTransitions: [
            {
              storageClass: s3.StorageClass.GLACIER,
              transitionAfter: cdk.Duration.days(30),
            },
          ],
          // Delete old versions after 90 days
          noncurrentVersionExpiration: cdk.Duration.days(90),
        },
      ],
      cors: [
        {
          allowedMethods: [
            s3.HttpMethods.GET,
            s3.HttpMethods.PUT,
            s3.HttpMethods.POST,
          ],
          allowedOrigins: ['*'], // TODO: Restrict this in production
          allowedHeaders: ['*'],
          maxAge: 3000,
        },
      ],
      removalPolicy: props.environment === 'production'
        ? cdk.RemovalPolicy.RETAIN   // Keep production data
        : cdk.RemovalPolicy.DESTROY,  // Auto-cleanup for non-production
      autoDeleteObjects: props.environment !== 'production', // Auto-delete objects in non-production
    });

    // S3 bucket for PostgreSQL backups (CloudNativePG)
    const pgBackupBucket = new s3.Bucket(this, 'PGBackupBucket', {
      bucketName: `${props.config.clusterNamePrefix}-io-pg-backups-${props.environment}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true, // Always version backups
      lifecycleRules: [
        {
          id: 'DeleteOldBackups',
          enabled: true,
          expiration: cdk.Duration.days(30),
        },
        {
          id: 'TransitionToIA',
          enabled: true,
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(30),
            },
          ],
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN, // Always retain backups
    });

    // VPC for the cluster
    // Production: 3 NAT gateways (high availability)
    // Staging: 1 NAT gateway (cost optimization ~$64/month savings)
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 3,
      natGateways: props.environment === 'production' ? 3 : 1,
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
    vpc.publicSubnets.forEach((subnet, idx) => {
      cdk.Tags.of(subnet).add('kubernetes.io/role/elb', '1');
    });
    vpc.privateSubnets.forEach((subnet, idx) => {
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
      clusterName: props.clusterName,
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

    // TODO: Fix CloudFormation deletion issue with EKS cluster
    // Known limitation: CloudFormation stack deletion may fail due to missing eks:DeleteCluster permission
    // Workaround: Use `cdk destroy` or manually delete the cluster before stack deletion
    // Proper fix requires modifying the cluster creation role permissions, which is complex

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

    // Create managed node group for Karpenter controller
    // Note: Using ON_DEMAND for stability - Karpenter controller should not run on SPOT
    const karpenterNodeGroup = this.cluster.addNodegroupCapacity('KarpenterNodeGroup', {
      instanceTypes: [new ec2.InstanceType('t3.medium')],
      minSize: 2,
      maxSize: 3,
      desiredSize: 2,
      amiType: eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
      diskSize: 20,
      capacityType: eks.CapacityType.ON_DEMAND,
      labels: {
        'node-type': 'karpenter-controller',
      },
      taints: [
        {
          key: 'CriticalAddonsOnly',
          value: 'true',
          effect: eks.TaintEffect.NO_SCHEDULE,
        },
      ],
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
      roleName: `${props.clusterName}-ebs-csi`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonEBSCSIDriverPolicy'),
      ],
    });

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
    const ebsCsiPodIdentity = new eks.CfnPodIdentityAssociation(this, 'EbsCsiPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'kube-system',
      serviceAccount: 'ebs-csi-controller-sa',
      roleArn: ebsCsiRole.roleArn,
    });

    const ebsCsiAddon = new eks.CfnAddon(this, 'EbsCsiAddon', {
      clusterName: this.cluster.clusterName,
      addonName: 'aws-ebs-csi-driver',
      addonVersion: addonVersions.ebsCsi,
      resolveConflicts: 'OVERWRITE',
      serviceAccountRoleArn: ebsCsiRole.roleArn,
    });

    // Create gp3 storage classes for high-performance, cost-effective storage
    // gp3 provides 60% cost savings vs io2 with baseline 3000 IOPS + 125 MB/s
    const gp3StorageClass = this.cluster.addManifest('GP3StorageClass', {
      apiVersion: 'storage.k8s.io/v1',
      kind: 'StorageClass',
      metadata: {
        name: 'gp3',
        annotations: {
          'storageclass.kubernetes.io/is-default-class': 'true',
        },
      },
      provisioner: 'ebs.csi.aws.com',
      parameters: {
        type: 'gp3',
        iops: '3000',        // Baseline (free), can scale to 16000
        throughput: '125',    // MB/s (free), can scale to 1000
        encrypted: 'true',
        fsType: 'ext4',
      },
      volumeBindingMode: 'WaitForFirstConsumer',
      allowVolumeExpansion: true,
      reclaimPolicy: 'Delete',
    });

    // High-performance gp3 storage class optimized for PostgreSQL workloads
    const gp3PostgresStorageClass = this.cluster.addManifest('GP3PostgresStorageClass', {
      apiVersion: 'storage.k8s.io/v1',
      kind: 'StorageClass',
      metadata: {
        name: 'gp3-postgres',
      },
      provisioner: 'ebs.csi.aws.com',
      parameters: {
        type: 'gp3',
        iops: '5000',        // Higher IOPS for database workloads
        throughput: '250',    // Higher throughput for WAL writes
        encrypted: 'true',
        fsType: 'ext4',
      },
      volumeBindingMode: 'WaitForFirstConsumer',
      allowVolumeExpansion: true,
      reclaimPolicy: 'Delete',
    });

    // Optional: io2 storage class for mission-critical databases requiring ultra-low latency
    const io2PostgresStorageClass = this.cluster.addManifest('IO2PostgresStorageClass', {
      apiVersion: 'storage.k8s.io/v1',
      kind: 'StorageClass',
      metadata: {
        name: 'io2-postgres',
      },
      provisioner: 'ebs.csi.aws.com',
      parameters: {
        type: 'io2',
        iops: '10000',       // Higher baseline for io2 (consistent <1ms latency)
        encrypted: 'true',
        fsType: 'ext4',
      },
      volumeBindingMode: 'WaitForFirstConsumer',
      allowVolumeExpansion: true,
      reclaimPolicy: 'Delete',
    });

    // Storage classes depend on EBS CSI driver being installed
    // Chain all Kubernetes manifests sequentially to avoid Lambda rate limiting
    // The CDK creates AwsAuth automatically; chain everything after it
    // Full chain: AwsAuth → gp3 → gp3-postgres → io2-postgres → rem → observability → postgres → karpenter
    gp3StorageClass.node.addDependency(ebsCsiAddon);
    gp3StorageClass.node.addDependency(this.cluster.awsAuth);  // Start after AwsAuth
    gp3PostgresStorageClass.node.addDependency(gp3StorageClass);
    io2PostgresStorageClass.node.addDependency(gp3PostgresStorageClass);

    // Install EKS Pod Identity Agent addon
    // Required for Pod Identity authentication (modern alternative to IRSA)
    new eks.CfnAddon(this, 'PodIdentityAgentAddon', {
      clusterName: this.cluster.clusterName,
      addonName: 'eks-pod-identity-agent',
      addonVersion: addonVersions.podIdentityAgent,
      resolveConflicts: 'OVERWRITE',
    });

    // EBS CSI addon must wait for Pod Identity association
    ebsCsiAddon.addDependency(ebsCsiPodIdentity);

    // Create IAM role for AWS Load Balancer Controller (deployed via ArgoCD)
    const albControllerRole = new iam.Role(this, 'ALBControllerRole', {
      roleName: `${props.clusterName}-alb-controller`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      description: 'IAM role for AWS Load Balancer Controller',
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

    // Attach AWS Load Balancer Controller policy
    // Download from: https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/main/docs/install/iam_policy.json
    const albControllerPolicy = new iam.ManagedPolicy(this, 'ALBControllerPolicy', {
      managedPolicyName: `${props.clusterName}-alb-controller-policy`,
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'iam:CreateServiceLinkedRole',
          ],
          resources: ['*'],
          conditions: {
            StringEquals: {
              'iam:AWSServiceName': 'elasticloadbalancing.amazonaws.com',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:DescribeAccountAttributes',
            'ec2:DescribeAddresses',
            'ec2:DescribeAvailabilityZones',
            'ec2:DescribeInternetGateways',
            'ec2:DescribeVpcs',
            'ec2:DescribeVpcPeeringConnections',
            'ec2:DescribeSubnets',
            'ec2:DescribeSecurityGroups',
            'ec2:DescribeInstances',
            'ec2:DescribeNetworkInterfaces',
            'ec2:DescribeTags',
            'ec2:GetCoipPoolUsage',
            'ec2:DescribeCoipPools',
            'elasticloadbalancing:DescribeLoadBalancers',
            'elasticloadbalancing:DescribeLoadBalancerAttributes',
            'elasticloadbalancing:DescribeListeners',
            'elasticloadbalancing:DescribeListenerCertificates',
            'elasticloadbalancing:DescribeListenerAttributes',
            'elasticloadbalancing:DescribeSSLPolicies',
            'elasticloadbalancing:DescribeRules',
            'elasticloadbalancing:DescribeTargetGroups',
            'elasticloadbalancing:DescribeTargetGroupAttributes',
            'elasticloadbalancing:DescribeTargetHealth',
            'elasticloadbalancing:DescribeTags',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'cognito-idp:DescribeUserPoolClient',
            'acm:ListCertificates',
            'acm:DescribeCertificate',
            'iam:ListServerCertificates',
            'iam:GetServerCertificate',
            'waf-regional:GetWebACL',
            'waf-regional:GetWebACLForResource',
            'waf-regional:AssociateWebACL',
            'waf-regional:DisassociateWebACL',
            'wafv2:GetWebACL',
            'wafv2:GetWebACLForResource',
            'wafv2:AssociateWebACL',
            'wafv2:DisassociateWebACL',
            'shield:GetSubscriptionState',
            'shield:DescribeProtection',
            'shield:CreateProtection',
            'shield:DeleteProtection',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:AuthorizeSecurityGroupIngress',
            'ec2:RevokeSecurityGroupIngress',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateSecurityGroup',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateTags',
          ],
          resources: ['arn:aws:ec2:*:*:security-group/*'],
          conditions: {
            StringEquals: {
              'ec2:CreateAction': 'CreateSecurityGroup',
            },
            Null: {
              'aws:RequestTag/elbv2.k8s.aws/cluster': 'false',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateTags',
            'ec2:DeleteTags',
          ],
          resources: ['arn:aws:ec2:*:*:security-group/*'],
          conditions: {
            Null: {
              'aws:RequestTag/elbv2.k8s.aws/cluster': 'true',
              'aws:ResourceTag/elbv2.k8s.aws/cluster': 'false',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:AuthorizeSecurityGroupIngress',
            'ec2:RevokeSecurityGroupIngress',
            'ec2:DeleteSecurityGroup',
          ],
          resources: ['*'],
          conditions: {
            Null: {
              'aws:ResourceTag/elbv2.k8s.aws/cluster': 'false',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:CreateLoadBalancer',
            'elasticloadbalancing:CreateTargetGroup',
          ],
          resources: ['*'],
          conditions: {
            Null: {
              'aws:RequestTag/elbv2.k8s.aws/cluster': 'false',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:AddTags',
          ],
          resources: [
            'arn:aws:elasticloadbalancing:*:*:targetgroup/*/*',
            'arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*',
            'arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*',
          ],
          conditions: {
            Null: {
              'aws:RequestTag/elbv2.k8s.aws/cluster': 'false',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:CreateListener',
            'elasticloadbalancing:DeleteListener',
            'elasticloadbalancing:CreateRule',
            'elasticloadbalancing:DeleteRule',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:AddListenerCertificates',
            'elasticloadbalancing:RemoveListenerCertificates',
            'elasticloadbalancing:ModifyListener',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:ModifyLoadBalancerAttributes',
            'elasticloadbalancing:ModifyTargetGroup',
            'elasticloadbalancing:ModifyTargetGroupAttributes',
            'elasticloadbalancing:DeleteLoadBalancer',
            'elasticloadbalancing:DeleteTargetGroup',
          ],
          resources: ['*'],
          conditions: {
            Null: {
              'aws:ResourceTag/elbv2.k8s.aws/cluster': 'false',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:AddTags',
            'elasticloadbalancing:RemoveTags',
          ],
          resources: [
            'arn:aws:elasticloadbalancing:*:*:targetgroup/*/*',
            'arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*',
            'arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*',
          ],
          conditions: {
            Null: {
              'aws:RequestTag/elbv2.k8s.aws/cluster': 'true',
              'aws:ResourceTag/elbv2.k8s.aws/cluster': 'false',
            },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:AddTags',
            'elasticloadbalancing:RemoveTags',
          ],
          resources: [
            'arn:aws:elasticloadbalancing:*:*:listener/net/*/*/*',
            'arn:aws:elasticloadbalancing:*:*:listener/app/*/*/*',
            'arn:aws:elasticloadbalancing:*:*:listener-rule/net/*/*/*',
            'arn:aws:elasticloadbalancing:*:*:listener-rule/app/*/*/*',
          ],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:ModifyRule',
            'elasticloadbalancing:SetIpAddressType',
            'elasticloadbalancing:SetSecurityGroups',
            'elasticloadbalancing:SetSubnets',
            'elasticloadbalancing:DeleteLoadBalancer',
            'elasticloadbalancing:DeleteTargetGroup',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:RegisterTargets',
            'elasticloadbalancing:DeregisterTargets',
          ],
          resources: ['arn:aws:elasticloadbalancing:*:*:targetgroup/*/*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:SetWebAcl',
            'elasticloadbalancing:ModifyListener',
            'elasticloadbalancing:AddListenerCertificates',
            'elasticloadbalancing:RemoveListenerCertificates',
            'elasticloadbalancing:ModifyRule',
          ],
          resources: ['*'],
        }),
      ],
    });

    albControllerRole.addManagedPolicy(albControllerPolicy);

    // Create EC2 Spot Service-Linked Role (required for Karpenter to use Spot instances)
    // This is a one-time setup per AWS account, idempotent if already exists
    const spotServiceLinkedRole = new iam.CfnServiceLinkedRole(this, 'SpotServiceLinkedRole', {
      awsServiceName: 'spot.amazonaws.com',
      description: 'Service-linked role for EC2 Spot instances (required by Karpenter)',
    });

    // Create IAM role for Karpenter (deployed via ArgoCD)
    const karpenterRole = new iam.Role(this, 'KarpenterRole', {
      roleName: `${props.clusterName}-karpenter`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      description: 'IAM role for Karpenter autoscaler',
    });

    karpenterRole.assumeRolePolicy?.addStatements(
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

    const karpenterPolicy = new iam.ManagedPolicy(this, 'KarpenterPolicy', {
      managedPolicyName: `${props.clusterName}-karpenter-policy`,
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateFleet',
            'ec2:CreateLaunchTemplate',
            'ec2:CreateTags',
            'ec2:DescribeAvailabilityZones',
            'ec2:DescribeImages',
            'ec2:DescribeInstances',
            'ec2:DescribeInstanceTypeOfferings',
            'ec2:DescribeInstanceTypes',
            'ec2:DescribeLaunchTemplates',
            'ec2:DescribeSecurityGroups',
            'ec2:DescribeSpotPriceHistory',
            'ec2:DescribeSubnets',
            'ec2:DeleteLaunchTemplate',
            'ec2:RunInstances',
            'ec2:TerminateInstances',
            'iam:PassRole',
            'iam:GetInstanceProfile',
            'iam:CreateInstanceProfile',
            'iam:AddRoleToInstanceProfile',
            'iam:DeleteInstanceProfile',
            'iam:RemoveRoleFromInstanceProfile',
            'iam:TagInstanceProfile',
            'ssm:GetParameter',
            'pricing:GetProducts',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'eks:DescribeCluster',
          ],
          resources: [this.cluster.clusterArn],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'sqs:DeleteMessage',
            'sqs:GetQueueUrl',
            'sqs:ReceiveMessage',
          ],
          resources: [`arn:aws:sqs:${this.region}:${this.account}:${props.clusterName}-karpenter-interruption`],
        }),
      ],
    });

    karpenterRole.addManagedPolicy(karpenterPolicy);

    // Grant Karpenter role permissions to manage node IAM instance profile
    const nodeRole = this.cluster.defaultNodegroup
      ? this.cluster.defaultNodegroup.role
      : karpenterNodeGroup.role;

    karpenterRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: [nodeRole.roleArn],
      })
    );

    // ============================================================
    // REM APPLICATION IAM ROLE
    // ============================================================

    // Create IAM role for REM application pods (single role for entire app)
    // This role is used by the 'rem-app' ServiceAccount in the 'rem' namespace
    // All REM pods (API, workers, MCP, etc.) use this ServiceAccount
    const appPodRole = new iam.Role(this, 'AppPodRole', {
      roleName: `${props.clusterName}-app`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      description: 'Pod identity role for REM application (S3, SQS, SSM, Secrets Manager)',
    });

    appPodRole.assumeRolePolicy?.addStatements(
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

    // Grant S3 access
    appBucket.grantReadWrite(appPodRole);

    // Grant SSM Parameter Store access (for app config)
    appPodRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'ssm:GetParameter',
          'ssm:GetParameters',
          'ssm:GetParametersByPath',
        ],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter/${props.config.clusterNamePrefix}/*`,
        ],
      })
    );

    // Grant Secrets Manager access (for DB credentials, API keys)
    appPodRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'secretsmanager:GetSecretValue',
          'secretsmanager:DescribeSecret',
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:${props.config.clusterNamePrefix}/*`,
        ],
      })
    );

    // Grant CloudWatch Logs and Metrics access
    appPodRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'cloudwatch:PutMetricData',
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
        ],
        resources: ['*'],
      })
    );

    // Grant X-Ray access (for OpenTelemetry)
    appPodRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'xray:PutTraceSegments',
          'xray:PutTelemetryRecords',
        ],
        resources: ['*'],
      })
    );

    // Create pod identity association for REM application
    // This allows the 'rem-app' service account in the app namespace to assume the role
    const remAppPodIdentity = new eks.CfnPodIdentityAssociation(this, 'REMAppPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: props.config.appNamespace,
      serviceAccount: 'rem-app',
      roleArn: appPodRole.roleArn,
    });

    // Create the application namespace (configurable, defaults to 'rem')
    // Chain namespaces sequentially to avoid Lambda rate limiting
    const remNamespace = this.cluster.addManifest('REMNamespace', {
      apiVersion: 'v1',
      kind: 'Namespace',
      metadata: { name: props.config.appNamespace },
    });
    remNamespace.node.addDependency(io2PostgresStorageClass);

    // Create the 'rem-app' ServiceAccount in the app namespace
    const remAppServiceAccount = this.cluster.addManifest('REMAppServiceAccount', {
      apiVersion: 'v1',
      kind: 'ServiceAccount',
      metadata: {
        name: 'rem-app',
        namespace: props.config.appNamespace,
      },
    });

    remAppServiceAccount.node.addDependency(remNamespace);
    remAppPodIdentity.node.addDependency(remAppServiceAccount);

    // Export the REM app role for use by other stacks (e.g., FileQueueStack)
    this.remAppRole = appPodRole;

    // ============================================================
    // FILE PROCESSING QUEUE (S3 + SQS)
    // ============================================================

    // Dead Letter Queue for failed file processing
    const fileProcessingDLQ = new cdk.aws_sqs.Queue(this, 'FileProcessingDLQ', {
      queueName: `${props.clusterName}-file-processing-dlq-${props.environment}`,
      retentionPeriod: cdk.Duration.days(14), // Keep failed messages for 2 weeks
      encryption: cdk.aws_sqs.QueueEncryption.SQS_MANAGED,
    });

    // Main file processing queue
    const fileProcessingQueue = new cdk.aws_sqs.Queue(this, 'FileProcessingQueue', {
      queueName: `${props.clusterName}-file-processing-${props.environment}`,
      visibilityTimeout: cdk.Duration.minutes(5), // Time to process + delete message
      receiveMessageWaitTime: cdk.Duration.seconds(20), // Long polling
      retentionPeriod: cdk.Duration.days(4),
      encryption: cdk.aws_sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: {
        queue: fileProcessingDLQ,
        maxReceiveCount: 3, // After 3 failed attempts, move to DLQ
      },
    });

    // S3 ObjectCreated event → SQS queue for file processing
    appBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new cdk.aws_s3_notifications.SqsDestination(fileProcessingQueue)
    );

    // Grant REM app role access to application bucket and file processing queue
    appBucket.grantReadWrite(appPodRole);
    fileProcessingQueue.grantConsumeMessages(appPodRole);
    fileProcessingDLQ.grantConsumeMessages(appPodRole);

    // ============================================================
    // OTEL COLLECTOR IAM ROLE (Observability Namespace)
    // ============================================================

    // Create IAM role for OpenTelemetry Collector
    const otelCollectorRole = new iam.Role(this, 'OTELCollectorRole', {
      roleName: `${props.clusterName}-otel-collector`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      description: 'Pod identity role for OTEL collector to send to X-Ray and CloudWatch',
    });

    otelCollectorRole.assumeRolePolicy?.addStatements(
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

    // Grant X-Ray permissions
    otelCollectorRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('AWSXRayDaemonWriteAccess')
    );

    // Grant CloudWatch permissions
    otelCollectorRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchAgentServerPolicy')
    );

    // Create pod identity association for OTEL collector
    const otelCollectorPodIdentity = new eks.CfnPodIdentityAssociation(this, 'OTELCollectorPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'observability',
      serviceAccount: 'otel-collector',
      roleArn: otelCollectorRole.roleArn,
    });

    // Create the 'observability' namespace
    // Chain after remNamespace to avoid Lambda rate limiting
    const observabilityNamespace = this.cluster.addManifest('ObservabilityNamespace', {
      apiVersion: 'v1',
      kind: 'Namespace',
      metadata: { name: 'observability' },
    });
    observabilityNamespace.node.addDependency(remNamespace);

    // Create the 'otel-collector' ServiceAccount
    const otelCollectorServiceAccount = this.cluster.addManifest('OTELCollectorServiceAccount', {
      apiVersion: 'v1',
      kind: 'ServiceAccount',
      metadata: {
        name: 'otel-collector',
        namespace: 'observability',
      },
    });

    otelCollectorServiceAccount.node.addDependency(observabilityNamespace);
    otelCollectorPodIdentity.node.addDependency(otelCollectorServiceAccount);

    // ============================================================
    // CLOUDNATIVEPG BACKUP IAM ROLE (postgres-cluster Namespace)
    // ============================================================

    // Create IAM role for CloudNativePG backups to S3
    const cnpgBackupRole = new iam.Role(this, 'CNPGBackupRole', {
      roleName: `${props.clusterName}-cnpg-backup`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      description: 'Pod identity role for CloudNativePG to write backups to S3',
    });

    cnpgBackupRole.assumeRolePolicy?.addStatements(
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

    // Grant S3 access for PostgreSQL backups
    pgBackupBucket.grantReadWrite(cnpgBackupRole);

    // Create pod identity association for CloudNativePG backups
    // NOTE: CNPG creates a service account named after the cluster (e.g., rem-postgres)
    // The postgres runs in the APP namespace, not a separate postgres-cluster namespace
    const cnpgBackupPodIdentity = new eks.CfnPodIdentityAssociation(this, 'CNPGBackupPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: props.config.appNamespace,  // Postgres runs in app namespace
      serviceAccount: `${props.config.clusterNamePrefix}-postgres`,  // CNPG creates SA with cluster name
      roleArn: cnpgBackupRole.roleArn,
    });

    // Pod identity depends on the namespace existing
    cnpgBackupPodIdentity.node.addDependency(remAppServiceAccount);

    // ============================================================
    // EXTERNAL SECRETS OPERATOR IAM ROLE
    // ============================================================

    // Create IAM role for External Secrets Operator
    const externalSecretsRole = new iam.Role(this, 'ExternalSecretsRole', {
      roleName: `${props.clusterName}-external-secrets`,
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

    // Grant Secrets Manager access
    externalSecretsRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'secretsmanager:GetSecretValue',
          'secretsmanager:DescribeSecret',
          'secretsmanager:ListSecrets',
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:/carrier/*`,
        ],
      })
    );

    // Grant Parameter Store access for secrets
    externalSecretsRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'ssm:GetParameter',
          'ssm:GetParameters',
          'ssm:GetParametersByPath',
        ],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter/${props.config.clusterNamePrefix}/*`,
        ],
      })
    );

    // Create pod identity association for External Secrets
    const externalSecretsPodIdentity = new eks.CfnPodIdentityAssociation(this, 'ExternalSecretsPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'external-secrets-system',
      serviceAccount: 'external-secrets',
      roleArn: externalSecretsRole.roleArn,
    });

    // ============================================================
    // CLOUDWATCH LOG GROUPS
    // ============================================================

    // Create log group for OTEL collector with retention
    const otelLogGroup = new logs.LogGroup(this, 'OTELLogGroup', {
      logGroupName: `/aws/eks/${props.clusterName}/otel`,
      retention: props.environment === 'production'
        ? logs.RetentionDays.ONE_MONTH
        : logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Create log group for application logs
    const appLogGroup = new logs.LogGroup(this, 'AppLogGroup', {
      logGroupName: `/aws/eks/${props.clusterName}/application`,
      retention: props.environment === 'production'
        ? logs.RetentionDays.ONE_MONTH
        : logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Create SQS queue for Karpenter interruption handling
    // This is used by Karpenter to handle spot instance interruptions
    const karpenterQueue = new cdk.aws_sqs.Queue(this, 'KarpenterInterruptionQueue', {
      queueName: `${props.clusterName}-karpenter-interruption`,
      retentionPeriod: cdk.Duration.days(14),
    });

    // ==================================================================
    // PLATFORM COMPONENTS - Deployed as part of cluster infrastructure
    // ==================================================================

    // Create pod identity association for Karpenter
    const karpenterPodIdentity = new eks.CfnPodIdentityAssociation(this, 'KarpenterPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'karpenter',
      serviceAccount: 'karpenter',
      roleArn: karpenterRole.roleArn,
    });

    // NOTE: AWS Load Balancer Controller removed from CDK
    // Reason: ALB Controller is application-layer infrastructure (ingress)
    // Karpenter is more fundamental (node provisioning)
    // Install ALB Controller via kubectl/ArgoCD after cluster and Karpenter are ready
    // See: worker-cluster/platform-addons/aws-load-balancer-controller/
    //
    // The IAM role and pod identity association are still created by CDK

    // Create pod identity association for AWS Load Balancer Controller
    // (used by kubectl/ArgoCD bootstrap)
    const albControllerPodIdentity = new eks.CfnPodIdentityAssociation(this, 'ALBControllerPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'kube-system',
      serviceAccount: 'aws-load-balancer-controller',
      roleArn: albControllerRole.roleArn,
    });

    // ============================================================
    // KEDA OPERATOR IAM ROLE (for SQS-based autoscaling)
    // ============================================================
    // KEDA operator needs SQS permissions to poll queue depth for scaling decisions.
    // When using TriggerAuthentication with identityOwner: keda, the KEDA operator
    // provides credentials, not the workload.

    const kedaRole = new iam.Role(this, 'KEDARole', {
      roleName: `${props.clusterName}-keda`,
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal('pods.eks.amazonaws.com')
      ),
      description: 'Pod identity role for KEDA operator to access SQS for autoscaling',
    });

    kedaRole.assumeRolePolicy?.addStatements(
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

    // Grant KEDA SQS permissions to read queue attributes (for scaling decisions)
    kedaRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'sqs:GetQueueAttributes',
          'sqs:GetQueueUrl',
        ],
        resources: [fileProcessingQueue.queueArn],
      })
    );

    // Create pod identity association for KEDA operator
    // Note: Service account name is 'keda-operator' as created by the KEDA Helm chart
    const kedaPodIdentity = new eks.CfnPodIdentityAssociation(this, 'KEDAPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'keda',
      serviceAccount: 'keda-operator',
      roleArn: kedaRole.roleArn,
    });

    // Deploy Karpenter via Helm for node autoscaling
    // Karpenter is fundamental infrastructure - it provisions nodes for all workloads
    // Chain after observabilityNamespace to avoid Lambda rate limiting
    const karpenterNamespace = this.cluster.addManifest('KarpenterNamespace', {
      apiVersion: 'v1',
      kind: 'Namespace',
      metadata: { name: 'karpenter' },
    });
    karpenterNamespace.node.addDependency(observabilityNamespace);

    const karpenter = this.cluster.addHelmChart('Karpenter', {
      chart: 'karpenter',
      repository: 'oci://public.ecr.aws/karpenter/karpenter',
      namespace: 'karpenter',
      version: '1.0.8',
      values: {
        settings: {
          clusterName: props.clusterName,
          clusterEndpoint: this.cluster.clusterEndpoint,
          interruptionQueue: karpenterQueue.queueName,
        },
        replicas: props.environment === 'production' ? 2 : 1,
        // Run Karpenter on dedicated managed nodes
        tolerations: [
          {
            key: 'CriticalAddonsOnly',
            operator: 'Exists',
            effect: 'NoSchedule',
          },
        ],
        nodeSelector: {
          'node-type': 'karpenter-controller',
        },
        // Service account for pod identity
        serviceAccount: {
          name: 'karpenter',
          annotations: {},
        },
      },
    });

    karpenter.node.addDependency(karpenterNamespace);
    karpenter.node.addDependency(karpenterPodIdentity);

    // Create default Karpenter NodePool for general workloads
    const defaultNodePool = this.cluster.addManifest('KarpenterDefaultNodePool', {
      apiVersion: 'karpenter.sh/v1',
      kind: 'NodePool',
      metadata: {
        name: 'default',
      },
      spec: {
        template: {
          spec: {
            requirements: [
              {
                key: 'kubernetes.io/arch',
                operator: 'In',
                values: ['amd64'],
              },
              {
                key: 'kubernetes.io/os',
                operator: 'In',
                values: ['linux'],
              },
              {
                key: 'karpenter.sh/capacity-type',
                operator: 'In',
                values: props.environment === 'production'
                  ? ['on-demand']
                  : ['spot', 'on-demand'],
              },
              {
                key: 'karpenter.k8s.aws/instance-category',
                operator: 'In',
                values: ['c', 'm', 't'],
              },
              {
                key: 'karpenter.k8s.aws/instance-generation',
                operator: 'Gt',
                values: ['5'],
              },
            ],
            nodeClassRef: {
              group: 'karpenter.k8s.aws',
              kind: 'EC2NodeClass',
              name: 'default',
            },
            expireAfter: props.environment === 'production' ? '720h' : '168h',
          },
        },
        limits: {
          cpu: props.environment === 'production' ? '1000' : '100',
          memory: props.environment === 'production' ? '1000Gi' : '100Gi',
        },
        disruption: {
          consolidationPolicy: 'WhenEmptyOrUnderutilized',
          consolidateAfter: '1m',
        },
      },
    });

    defaultNodePool.node.addDependency(karpenter);

    // Create default Karpenter EC2NodeClass
    const defaultNodeClass = this.cluster.addManifest('KarpenterDefaultNodeClass', {
      apiVersion: 'karpenter.k8s.aws/v1',
      kind: 'EC2NodeClass',
      metadata: {
        name: 'default',
      },
      spec: {
        amiFamily: 'AL2023',
        amiSelectorTerms: [
          {
            alias: 'al2023@latest',
          },
        ],
        role: nodeRole.roleName,
        subnetSelectorTerms: [
          {
            tags: {
              'Name': '*Private*',
            },
          },
        ],
        securityGroupSelectorTerms: [
          {
            tags: {
              'aws:eks:cluster-name': props.clusterName,
            },
          },
        ],
        userData: cdk.Fn.base64(
          [
            '#!/bin/bash',
            'echo "Running custom user data"',
            '# Add any custom bootstrapping here',
          ].join('\n')
        ),
        blockDeviceMappings: [
          {
            deviceName: '/dev/xvda',
            ebs: {
              volumeSize: '100Gi',
              volumeType: 'gp3',
              encrypted: true,
              deleteOnTermination: true,
            },
          },
        ],
        metadataOptions: {
          httpEndpoint: 'enabled',
          httpProtocolIPv6: 'disabled',
          httpPutResponseHopLimit: 1,
          httpTokens: 'required',
        },
      },
    });

    defaultNodeClass.node.addDependency(karpenter);

    // Outputs
    new cdk.CfnOutput(this, 'ClusterName', {
      value: this.cluster.clusterName,
      description: 'Worker cluster name',
      exportName: `${props.clusterName}-cluster-name`,
    });

    new cdk.CfnOutput(this, 'VpcId', {
      value: vpc.vpcId,
      description: 'VPC ID for the cluster',
      exportName: `${props.clusterName}-vpc-id`,
    });

    new cdk.CfnOutput(this, 'AppBucketName', {
      value: appBucket.bucketName,
      description: 'S3 bucket for application storage (uploads, files, and general data)',
      exportName: `${props.clusterName}-bucket-name`,
    });

    new cdk.CfnOutput(this, 'AppPodRoleArn', {
      value: appPodRole.roleArn,
      description: 'IAM role ARN for application pods',
      exportName: `${props.clusterName}-app-pod-role-arn`,
    });

    new cdk.CfnOutput(this, 'ALBControllerRoleArn', {
      value: albControllerRole.roleArn,
      description: 'IAM role ARN for AWS Load Balancer Controller (use with kubectl/ArgoCD bootstrap)',
      exportName: `${props.clusterName}-alb-controller-role-arn`,
    });

    new cdk.CfnOutput(this, 'KarpenterRoleArn', {
      value: karpenterRole.roleArn,
      description: 'IAM role ARN for Karpenter',
      exportName: `${props.clusterName}-karpenter-role-arn`,
    });

    new cdk.CfnOutput(this, 'KarpenterQueueUrl', {
      value: karpenterQueue.queueUrl,
      description: 'SQS queue URL for Karpenter interruption handling',
      exportName: `${props.clusterName}-karpenter-queue-url`,
    });

    new cdk.CfnOutput(this, 'ClusterEndpoint', {
      value: this.cluster.clusterEndpoint,
      description: 'EKS cluster API endpoint',
      exportName: `${props.clusterName}-endpoint`,
    });

    new cdk.CfnOutput(this, 'PGBackupBucketName', {
      value: pgBackupBucket.bucketName,
      description: 'S3 bucket for PostgreSQL backups',
      exportName: `${props.clusterName}-pg-backup-bucket`,
    });

    new cdk.CfnOutput(this, 'OTELCollectorRoleArn', {
      value: otelCollectorRole.roleArn,
      description: 'IAM role ARN for OTEL collector',
      exportName: `${props.clusterName}-otel-collector-role-arn`,
    });

    new cdk.CfnOutput(this, 'CNPGBackupRoleArn', {
      value: cnpgBackupRole.roleArn,
      description: 'IAM role ARN for CloudNativePG backups',
      exportName: `${props.clusterName}-cnpg-backup-role-arn`,
    });

    new cdk.CfnOutput(this, 'ExternalSecretsRoleArn', {
      value: externalSecretsRole.roleArn,
      description: 'IAM role ARN for External Secrets Operator',
      exportName: `${props.clusterName}-external-secrets-role-arn`,
    });

    new cdk.CfnOutput(this, 'KEDARoleArn', {
      value: kedaRole.roleArn,
      description: 'IAM role ARN for KEDA operator (SQS autoscaling)',
      exportName: `${props.clusterName}-keda-role-arn`,
    });

    new cdk.CfnOutput(this, 'OTELLogGroupName', {
      value: otelLogGroup.logGroupName,
      description: 'CloudWatch log group for OTEL collector',
      exportName: `${props.clusterName}-otel-log-group`,
    });

    new cdk.CfnOutput(this, 'AppLogGroupName', {
      value: appLogGroup.logGroupName,
      description: 'CloudWatch log group for applications',
      exportName: `${props.clusterName}-app-log-group`,
    });

    // FileUploadBucketName output removed - now using AppBucketName for all application storage

    new cdk.CfnOutput(this, 'FileProcessingQueueUrl', {
      value: fileProcessingQueue.queueUrl,
      description: 'SQS queue URL for file processing',
      exportName: `${props.clusterName}-file-queue-url`,
    });

    new cdk.CfnOutput(this, 'FileProcessingQueueArn', {
      value: fileProcessingQueue.queueArn,
      description: 'SQS queue ARN for KEDA scaling',
      exportName: `${props.clusterName}-file-queue-arn`,
    });

    new cdk.CfnOutput(this, 'FileProcessingDLQUrl', {
      value: fileProcessingDLQ.queueUrl,
      description: 'Dead letter queue URL for failed file processing',
      exportName: `${props.clusterName}-file-dlq-url`,
    });
  }
}
