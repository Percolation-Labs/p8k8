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

/**
 * EksClusterStack - Base EKS cluster infrastructure
 *
 * This stack creates:
 * - VPC with public/private subnets
 * - EKS cluster with managed node group
 * - Core EKS addons (VPC-CNI, CoreDNS, KubeProxy, EBS-CSI)
 * - S3 buckets (app data, PG backups)
 * - SQS queues (file processing, Karpenter interruption)
 * - IAM roles for all components
 * - CloudWatch log groups
 *
 * This stack is SEPARATE from EksAddonsStack so that:
 * - If cluster creation fails, only this stack rolls back
 * - If addons fail, cluster remains intact
 */
export interface EksClusterStackProps extends cdk.StackProps {
  clusterName: string;
  environment: string;
  ecrRepository: ecr.Repository;
  config: ClusterConfig;
}

export class EksClusterStack extends cdk.Stack {
  // Exported resources for EksAddonsStack
  public readonly cluster: eks.Cluster;
  public readonly vpc: ec2.Vpc;
  public readonly nodeRole: iam.IRole;
  public readonly appBucket: s3.Bucket;
  public readonly pgBackupBucket: s3.Bucket;
  public readonly fileProcessingQueue: cdk.aws_sqs.Queue;
  public readonly fileProcessingDLQ: cdk.aws_sqs.Queue;
  public readonly karpenterQueue: cdk.aws_sqs.Queue;

  // IAM roles (created here, Pod Identity associations in AddonsStack)
  public readonly appPodRole: iam.Role;
  public readonly karpenterRole: iam.Role;
  public readonly albControllerRole: iam.Role;
  public readonly otelCollectorRole: iam.Role;
  public readonly cnpgBackupRole: iam.Role;
  public readonly externalSecretsRole: iam.Role;
  public readonly ebsCsiRole: iam.Role;
  public readonly kedaOperatorRole: iam.Role;

  constructor(scope: Construct, id: string, props: EksClusterStackProps) {
    super(scope, id, props);

    // ============================================================
    // S3 BUCKETS
    // ============================================================

    this.appBucket = new s3.Bucket(this, 'AppBucket', {
      bucketName: `${props.config.clusterNamePrefix}-io-${props.environment}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      lifecycleRules: [
        {
          noncurrentVersionTransitions: [
            {
              storageClass: s3.StorageClass.GLACIER,
              transitionAfter: cdk.Duration.days(30),
            },
          ],
          noncurrentVersionExpiration: cdk.Duration.days(90),
        },
      ],
      cors: [
        {
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT, s3.HttpMethods.POST],
          allowedOrigins: ['*'],
          allowedHeaders: ['*'],
          maxAge: 3000,
        },
      ],
      removalPolicy: props.environment === 'production'
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: props.environment !== 'production',
    });

    this.pgBackupBucket = new s3.Bucket(this, 'PGBackupBucket', {
      bucketName: `${props.config.clusterNamePrefix}-io-pg-backups-${props.environment}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
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
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ============================================================
    // VPC
    // ============================================================

    this.vpc = new ec2.Vpc(this, 'Vpc', {
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
    this.vpc.publicSubnets.forEach((subnet) => {
      cdk.Tags.of(subnet).add('kubernetes.io/role/elb', '1');
    });
    this.vpc.privateSubnets.forEach((subnet) => {
      cdk.Tags.of(subnet).add('kubernetes.io/role/internal-elb', '1');
    });

    // ============================================================
    // EKS CLUSTER
    // ============================================================

    const k8sVersion = props.config.kubernetesVersion;
    const k8sVersionEnum = (eks.KubernetesVersion as any)[`V${k8sVersion.replace('.', '_')}`];
    const kubectlLayer = k8sVersion === '1.33'
      ? new KubectlV33Layer(this, 'KubectlLayer')
      : new KubectlV31Layer(this, 'KubectlLayer');

    const addonVersions = getAddonVersions(k8sVersion);

    this.cluster = new eks.Cluster(this, 'Cluster', {
      clusterName: props.clusterName,
      version: k8sVersionEnum,
      vpc: this.vpc,
      vpcSubnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
      defaultCapacity: 0,
      authenticationMode: eks.AuthenticationMode.API_AND_CONFIG_MAP,
      kubectlLayer: kubectlLayer,
    });

    // Grant admin access
    if (props.config.adminRoleArn) {
      new eks.CfnAccessEntry(this, 'AdminAccessEntry', {
        clusterName: this.cluster.clusterName,
        principalArn: props.config.adminRoleArn,
        type: 'STANDARD',
        accessPolicies: [{
          policyArn: 'arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy',
          accessScope: { type: 'cluster' },
        }],
      });
    }

    // Managed node group for Karpenter controller
    const karpenterNodeGroup = this.cluster.addNodegroupCapacity('KarpenterNodeGroup', {
      instanceTypes: [new ec2.InstanceType('t3.medium')],
      minSize: 2,
      maxSize: 3,
      desiredSize: 2,
      amiType: eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
      diskSize: 20,
      capacityType: eks.CapacityType.ON_DEMAND,
      labels: { 'node-type': 'karpenter-controller' },
      taints: [
        {
          key: 'CriticalAddonsOnly',
          value: 'true',
          effect: eks.TaintEffect.NO_SCHEDULE,
        },
      ],
    });

    this.nodeRole = karpenterNodeGroup.role;

    // ============================================================
    // EKS ADDONS (Core - must be in cluster stack)
    // ============================================================

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

    new eks.CfnAddon(this, 'PodIdentityAgentAddon', {
      clusterName: this.cluster.clusterName,
      addonName: 'eks-pod-identity-agent',
      addonVersion: addonVersions.podIdentityAgent,
      resolveConflicts: 'OVERWRITE',
    });

    // EBS CSI Driver with IAM role
    this.ebsCsiRole = new iam.Role(this, 'EbsCsiRole', {
      roleName: `${props.clusterName}-ebs-csi`,
      assumedBy: new iam.ServicePrincipal('pods.eks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonEBSCSIDriverPolicy'),
      ],
    });

    this.ebsCsiRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('pods.eks.amazonaws.com')],
        actions: ['sts:AssumeRole', 'sts:TagSession'],
        conditions: {
          'StringEquals': { 'aws:SourceAccount': this.account },
          'ArnEquals': {
            'aws:SourceArn': `arn:aws:eks:${this.region}:${this.account}:cluster/${this.cluster.clusterName}`,
          },
        },
      })
    );

    const ebsCsiPodIdentity = new eks.CfnPodIdentityAssociation(this, 'EbsCsiPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'kube-system',
      serviceAccount: 'ebs-csi-controller-sa',
      roleArn: this.ebsCsiRole.roleArn,
    });

    const ebsCsiAddon = new eks.CfnAddon(this, 'EbsCsiAddon', {
      clusterName: this.cluster.clusterName,
      addonName: 'aws-ebs-csi-driver',
      addonVersion: addonVersions.ebsCsi,
      resolveConflicts: 'OVERWRITE',
      serviceAccountRoleArn: this.ebsCsiRole.roleArn,
    });

    ebsCsiAddon.addDependency(ebsCsiPodIdentity);

    // ============================================================
    // SQS QUEUES
    // ============================================================

    this.fileProcessingDLQ = new cdk.aws_sqs.Queue(this, 'FileProcessingDLQ', {
      queueName: `${props.clusterName}-file-processing-dlq-${props.environment}`,
      retentionPeriod: cdk.Duration.days(14),
      encryption: cdk.aws_sqs.QueueEncryption.SQS_MANAGED,
    });

    this.fileProcessingQueue = new cdk.aws_sqs.Queue(this, 'FileProcessingQueue', {
      queueName: `${props.clusterName}-file-processing-${props.environment}`,
      visibilityTimeout: cdk.Duration.minutes(5),
      receiveMessageWaitTime: cdk.Duration.seconds(20),
      retentionPeriod: cdk.Duration.days(4),
      encryption: cdk.aws_sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: {
        queue: this.fileProcessingDLQ,
        maxReceiveCount: 3,
      },
    });

    // S3 event notification
    this.appBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new cdk.aws_s3_notifications.SqsDestination(this.fileProcessingQueue)
    );

    this.karpenterQueue = new cdk.aws_sqs.Queue(this, 'KarpenterInterruptionQueue', {
      queueName: `${props.clusterName}-karpenter-interruption`,
      retentionPeriod: cdk.Duration.days(14),
    });

    // ============================================================
    // IAM ROLES (Pod Identity associations in AddonsStack)
    // ============================================================

    // P8 Application Role
    this.appPodRole = this.createPodIdentityRole('AppPodRole', `${props.clusterName}-app`,
      'Pod identity role for P8 application (S3, SQS, SSM, Secrets Manager)');

    this.appBucket.grantReadWrite(this.appPodRole);
    this.fileProcessingQueue.grantConsumeMessages(this.appPodRole);
    this.fileProcessingDLQ.grantConsumeMessages(this.appPodRole);

    this.appPodRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath'],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter/${props.config.clusterNamePrefix}/*`],
    }));

    this.appPodRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
      resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:${props.config.clusterNamePrefix}/*`],
    }));

    this.appPodRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['cloudwatch:PutMetricData', 'logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: ['*'],
    }));

    this.appPodRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords'],
      resources: ['*'],
    }));

    // ALB Controller Role
    this.albControllerRole = this.createPodIdentityRole('ALBControllerRole', `${props.clusterName}-alb-controller`,
      'IAM role for AWS Load Balancer Controller');
    this.addAlbControllerPolicy(this.albControllerRole, props.clusterName);

    // Karpenter Role
    this.karpenterRole = this.createPodIdentityRole('KarpenterRole', `${props.clusterName}-karpenter`,
      'IAM role for Karpenter autoscaler');
    this.addKarpenterPolicy(this.karpenterRole, props.clusterName);

    this.karpenterRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['iam:PassRole'],
      resources: [this.nodeRole.roleArn],
    }));

    // OTEL Collector Role
    this.otelCollectorRole = this.createPodIdentityRole('OTELCollectorRole', `${props.clusterName}-otel-collector`,
      'Pod identity role for OTEL collector to send to X-Ray and CloudWatch');
    this.otelCollectorRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AWSXRayDaemonWriteAccess'));
    this.otelCollectorRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchAgentServerPolicy'));

    // CloudNativePG Backup Role
    this.cnpgBackupRole = this.createPodIdentityRole('CNPGBackupRole', `${props.clusterName}-cnpg-backup`,
      'Pod identity role for CloudNativePG to write backups to S3');
    this.pgBackupBucket.grantReadWrite(this.cnpgBackupRole);

    // External Secrets Role
    this.externalSecretsRole = this.createPodIdentityRole('ExternalSecretsRole', `${props.clusterName}-external-secrets`,
      'Pod identity role for External Secrets Operator');

    this.externalSecretsRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret', 'secretsmanager:ListSecrets'],
      resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:/${props.config.clusterNamePrefix}/*`],
    }));

    this.externalSecretsRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath'],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter/${props.config.clusterNamePrefix}/*`],
    }));

    // KEDA Operator Role (for SQS-based autoscaling)
    this.kedaOperatorRole = this.createPodIdentityRole('KEDAOperatorRole', `${props.clusterName}-keda-operator`,
      'Pod identity role for KEDA operator to read SQS queue metrics');

    this.kedaOperatorRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'sqs:GetQueueAttributes',
        'sqs:GetQueueUrl',
      ],
      resources: [
        this.fileProcessingQueue.queueArn,
        this.fileProcessingDLQ.queueArn,
      ],
    }));

    // ============================================================
    // CLOUDWATCH LOG GROUPS
    // ============================================================

    new logs.LogGroup(this, 'OTELLogGroup', {
      logGroupName: `/aws/eks/${props.clusterName}/otel`,
      retention: props.environment === 'production' ? logs.RetentionDays.ONE_MONTH : logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    new logs.LogGroup(this, 'AppLogGroup', {
      logGroupName: `/aws/eks/${props.clusterName}/application`,
      retention: props.environment === 'production' ? logs.RetentionDays.ONE_MONTH : logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ============================================================
    // NAMESPACES AND SERVICE ACCOUNTS
    // Pre-create these so PodIdentityAssociations in AddonsStack pass validation
    // ============================================================

    // P8 namespace and service account
    const p8Namespace = new eks.KubernetesManifest(this, 'P8Namespace', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'Namespace',
        metadata: { name: props.config.appNamespace },
      }],
    });

    const p8AppServiceAccount = new eks.KubernetesManifest(this, 'P8AppServiceAccount', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'ServiceAccount',
        metadata: {
          name: 'p8-app',
          namespace: props.config.appNamespace,
        },
      }],
    });
    p8AppServiceAccount.node.addDependency(p8Namespace);

    // Observability namespace and service account
    const observabilityNamespace = new eks.KubernetesManifest(this, 'ObservabilityNamespace', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'Namespace',
        metadata: { name: 'observability' },
      }],
    });
    observabilityNamespace.node.addDependency(p8Namespace);

    const otelCollectorServiceAccount = new eks.KubernetesManifest(this, 'OTELCollectorServiceAccount', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'ServiceAccount',
        metadata: {
          name: 'otel-collector',
          namespace: 'observability',
        },
      }],
    });
    otelCollectorServiceAccount.node.addDependency(observabilityNamespace);

    // Postgres namespace and service account
    const postgresNamespace = new eks.KubernetesManifest(this, 'PostgresClusterNamespace', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'Namespace',
        metadata: { name: 'postgres-cluster' },
      }],
    });
    postgresNamespace.node.addDependency(observabilityNamespace);

    const postgresBackupServiceAccount = new eks.KubernetesManifest(this, 'PostgresBackupServiceAccount', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'ServiceAccount',
        metadata: {
          name: 'postgres-backup',
          namespace: 'postgres-cluster',
        },
      }],
    });
    postgresBackupServiceAccount.node.addDependency(postgresNamespace);

    // Karpenter namespace and service account
    const karpenterNamespace = new eks.KubernetesManifest(this, 'KarpenterNamespace', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'Namespace',
        metadata: { name: 'karpenter' },
      }],
    });
    karpenterNamespace.node.addDependency(postgresNamespace);

    const karpenterServiceAccount = new eks.KubernetesManifest(this, 'KarpenterServiceAccount', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'ServiceAccount',
        metadata: {
          name: 'karpenter',
          namespace: 'karpenter',
        },
      }],
    });
    karpenterServiceAccount.node.addDependency(karpenterNamespace);

    // External Secrets namespace and service account
    const externalSecretsNamespace = new eks.KubernetesManifest(this, 'ExternalSecretsNamespace', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'Namespace',
        metadata: { name: 'external-secrets-system' },
      }],
    });
    externalSecretsNamespace.node.addDependency(karpenterNamespace);

    const externalSecretsServiceAccount = new eks.KubernetesManifest(this, 'ExternalSecretsServiceAccount', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'ServiceAccount',
        metadata: {
          name: 'external-secrets',
          namespace: 'external-secrets-system',
        },
      }],
    });
    externalSecretsServiceAccount.node.addDependency(externalSecretsNamespace);

    // ALB Controller service account in kube-system
    const albControllerServiceAccount = new eks.KubernetesManifest(this, 'ALBControllerServiceAccount', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'ServiceAccount',
        metadata: {
          name: 'aws-load-balancer-controller',
          namespace: 'kube-system',
        },
      }],
    });
    albControllerServiceAccount.node.addDependency(externalSecretsNamespace);

    // ArgoCD namespace
    const argocdNamespace = new eks.KubernetesManifest(this, 'ArgoCDNamespace', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'Namespace',
        metadata: { name: 'argocd' },
      }],
    });
    argocdNamespace.node.addDependency(externalSecretsNamespace);

    // KEDA namespace and service account (for SQS-based autoscaling)
    const kedaNamespace = new eks.KubernetesManifest(this, 'KEDANamespace', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'Namespace',
        metadata: { name: 'keda' },
      }],
    });
    kedaNamespace.node.addDependency(argocdNamespace);

    const kedaOperatorServiceAccount = new eks.KubernetesManifest(this, 'KEDAOperatorServiceAccount', {
      cluster: this.cluster,
      manifest: [{
        apiVersion: 'v1',
        kind: 'ServiceAccount',
        metadata: {
          name: 'keda-operator',
          namespace: 'keda',
        },
      }],
    });
    kedaOperatorServiceAccount.node.addDependency(kedaNamespace);

    // ============================================================
    // POD IDENTITY ASSOCIATIONS
    // ============================================================

    // P8 App Pod Identity
    const p8AppPodIdentity = new eks.CfnPodIdentityAssociation(this, 'P8AppPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: props.config.appNamespace,
      serviceAccount: 'p8-app',
      roleArn: this.appPodRole.roleArn,
    });
    p8AppPodIdentity.node.addDependency(p8AppServiceAccount);

    // OTEL Collector Pod Identity
    const otelCollectorPodIdentity = new eks.CfnPodIdentityAssociation(this, 'OTELCollectorPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'observability',
      serviceAccount: 'otel-collector',
      roleArn: this.otelCollectorRole.roleArn,
    });
    otelCollectorPodIdentity.node.addDependency(otelCollectorServiceAccount);

    // CNPG Backup Pod Identity
    const cnpgBackupPodIdentity = new eks.CfnPodIdentityAssociation(this, 'CNPGBackupPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'postgres-cluster',
      serviceAccount: 'postgres-backup',
      roleArn: this.cnpgBackupRole.roleArn,
    });
    cnpgBackupPodIdentity.node.addDependency(postgresBackupServiceAccount);

    // External Secrets Pod Identity
    const externalSecretsPodIdentity = new eks.CfnPodIdentityAssociation(this, 'ExternalSecretsPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'external-secrets-system',
      serviceAccount: 'external-secrets',
      roleArn: this.externalSecretsRole.roleArn,
    });
    externalSecretsPodIdentity.node.addDependency(externalSecretsServiceAccount);

    // ALB Controller Pod Identity
    const albControllerPodIdentity = new eks.CfnPodIdentityAssociation(this, 'ALBControllerPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'kube-system',
      serviceAccount: 'aws-load-balancer-controller',
      roleArn: this.albControllerRole.roleArn,
    });
    albControllerPodIdentity.node.addDependency(albControllerServiceAccount);

    // Karpenter Pod Identity
    const karpenterPodIdentity = new eks.CfnPodIdentityAssociation(this, 'KarpenterPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'karpenter',
      serviceAccount: 'karpenter',
      roleArn: this.karpenterRole.roleArn,
    });
    karpenterPodIdentity.node.addDependency(karpenterServiceAccount);

    // KEDA Operator Pod Identity
    const kedaOperatorPodIdentity = new eks.CfnPodIdentityAssociation(this, 'KEDAOperatorPodIdentity', {
      clusterName: this.cluster.clusterName,
      namespace: 'keda',
      serviceAccount: 'keda-operator',
      roleArn: this.kedaOperatorRole.roleArn,
    });
    kedaOperatorPodIdentity.node.addDependency(kedaOperatorServiceAccount);

    // ============================================================
    // OUTPUTS
    // ============================================================

    new cdk.CfnOutput(this, 'ClusterName', {
      value: this.cluster.clusterName,
      exportName: `${props.clusterName}-cluster-name`,
    });

    new cdk.CfnOutput(this, 'ClusterEndpoint', {
      value: this.cluster.clusterEndpoint,
      exportName: `${props.clusterName}-endpoint`,
    });

    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      exportName: `${props.clusterName}-vpc-id`,
    });

    new cdk.CfnOutput(this, 'AppBucketName', {
      value: this.appBucket.bucketName,
      exportName: `${props.clusterName}-bucket-name`,
    });

    new cdk.CfnOutput(this, 'FileProcessingQueueUrl', {
      value: this.fileProcessingQueue.queueUrl,
      exportName: `${props.clusterName}-file-queue-url`,
    });

    new cdk.CfnOutput(this, 'KarpenterQueueUrl', {
      value: this.karpenterQueue.queueUrl,
      exportName: `${props.clusterName}-karpenter-queue-url`,
    });

    new cdk.CfnOutput(this, 'AppPodRoleArn', {
      value: this.appPodRole.roleArn,
      exportName: `${props.clusterName}-app-pod-role-arn`,
    });

    new cdk.CfnOutput(this, 'KarpenterRoleArn', {
      value: this.karpenterRole.roleArn,
      exportName: `${props.clusterName}-karpenter-role-arn`,
    });

    new cdk.CfnOutput(this, 'ALBControllerRoleArn', {
      value: this.albControllerRole.roleArn,
      exportName: `${props.clusterName}-alb-controller-role-arn`,
    });
  }

  private createPodIdentityRole(id: string, roleName: string, description: string): iam.Role {
    const role = new iam.Role(this, id, {
      roleName,
      assumedBy: new iam.ServicePrincipal('pods.eks.amazonaws.com'),
      description,
    });

    role.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('pods.eks.amazonaws.com')],
        actions: ['sts:AssumeRole', 'sts:TagSession'],
        conditions: {
          'StringEquals': { 'aws:SourceAccount': this.account },
          'ArnEquals': {
            'aws:SourceArn': `arn:aws:eks:${this.region}:${this.account}:cluster/${this.cluster.clusterName}`,
          },
        },
      })
    );

    return role;
  }

  private addAlbControllerPolicy(role: iam.Role, clusterName: string): void {
    const policy = new iam.ManagedPolicy(this, 'ALBControllerPolicy', {
      managedPolicyName: `${clusterName}-alb-controller-policy`,
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['iam:CreateServiceLinkedRole'],
          resources: ['*'],
          conditions: { StringEquals: { 'iam:AWSServiceName': 'elasticloadbalancing.amazonaws.com' } },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:DescribeAccountAttributes', 'ec2:DescribeAddresses', 'ec2:DescribeAvailabilityZones',
            'ec2:DescribeInternetGateways', 'ec2:DescribeVpcs', 'ec2:DescribeVpcPeeringConnections',
            'ec2:DescribeSubnets', 'ec2:DescribeSecurityGroups', 'ec2:DescribeInstances',
            'ec2:DescribeNetworkInterfaces', 'ec2:DescribeTags', 'ec2:GetCoipPoolUsage', 'ec2:DescribeCoipPools',
            'ec2:GetSecurityGroupsForVpc', 'ec2:DescribeIpamPools', 'ec2:DescribeRouteTables',
            'elasticloadbalancing:DescribeLoadBalancers', 'elasticloadbalancing:DescribeLoadBalancerAttributes',
            'elasticloadbalancing:DescribeListeners', 'elasticloadbalancing:DescribeListenerCertificates',
            'elasticloadbalancing:DescribeListenerAttributes', 'elasticloadbalancing:DescribeSSLPolicies',
            'elasticloadbalancing:DescribeRules', 'elasticloadbalancing:DescribeTargetGroups',
            'elasticloadbalancing:DescribeTargetGroupAttributes', 'elasticloadbalancing:DescribeTargetHealth',
            'elasticloadbalancing:DescribeTags',
            'elasticloadbalancing:DescribeTrustStores', 'elasticloadbalancing:DescribeCapacityReservation',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'cognito-idp:DescribeUserPoolClient', 'acm:ListCertificates', 'acm:DescribeCertificate',
            'iam:ListServerCertificates', 'iam:GetServerCertificate', 'waf-regional:GetWebACL',
            'waf-regional:GetWebACLForResource', 'waf-regional:AssociateWebACL', 'waf-regional:DisassociateWebACL',
            'wafv2:GetWebACL', 'wafv2:GetWebACLForResource', 'wafv2:AssociateWebACL', 'wafv2:DisassociateWebACL',
            'shield:GetSubscriptionState', 'shield:DescribeProtection', 'shield:CreateProtection', 'shield:DeleteProtection',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ec2:AuthorizeSecurityGroupIngress', 'ec2:RevokeSecurityGroupIngress'],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ec2:CreateSecurityGroup'],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ec2:CreateTags'],
          resources: ['arn:aws:ec2:*:*:security-group/*'],
          conditions: {
            StringEquals: { 'ec2:CreateAction': 'CreateSecurityGroup' },
            Null: { 'aws:RequestTag/elbv2.k8s.aws/cluster': 'false' },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ec2:CreateTags', 'ec2:DeleteTags'],
          resources: ['arn:aws:ec2:*:*:security-group/*'],
          conditions: {
            Null: { 'aws:RequestTag/elbv2.k8s.aws/cluster': 'true', 'aws:ResourceTag/elbv2.k8s.aws/cluster': 'false' },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['ec2:AuthorizeSecurityGroupIngress', 'ec2:RevokeSecurityGroupIngress', 'ec2:DeleteSecurityGroup'],
          resources: ['*'],
          conditions: { Null: { 'aws:ResourceTag/elbv2.k8s.aws/cluster': 'false' } },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['elasticloadbalancing:CreateLoadBalancer', 'elasticloadbalancing:CreateTargetGroup'],
          resources: ['*'],
          conditions: { Null: { 'aws:RequestTag/elbv2.k8s.aws/cluster': 'false' } },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['elasticloadbalancing:AddTags'],
          resources: [
            'arn:aws:elasticloadbalancing:*:*:targetgroup/*/*',
            'arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*',
            'arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*',
          ],
          conditions: { Null: { 'aws:RequestTag/elbv2.k8s.aws/cluster': 'false' } },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:CreateListener', 'elasticloadbalancing:DeleteListener',
            'elasticloadbalancing:CreateRule', 'elasticloadbalancing:DeleteRule',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:AddListenerCertificates', 'elasticloadbalancing:RemoveListenerCertificates',
            'elasticloadbalancing:ModifyListener',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:ModifyLoadBalancerAttributes', 'elasticloadbalancing:ModifyTargetGroup',
            'elasticloadbalancing:ModifyTargetGroupAttributes', 'elasticloadbalancing:DeleteLoadBalancer',
            'elasticloadbalancing:DeleteTargetGroup',
          ],
          resources: ['*'],
          conditions: { Null: { 'aws:ResourceTag/elbv2.k8s.aws/cluster': 'false' } },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['elasticloadbalancing:AddTags', 'elasticloadbalancing:RemoveTags'],
          resources: [
            'arn:aws:elasticloadbalancing:*:*:targetgroup/*/*',
            'arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*',
            'arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*',
          ],
          conditions: {
            Null: { 'aws:RequestTag/elbv2.k8s.aws/cluster': 'true', 'aws:ResourceTag/elbv2.k8s.aws/cluster': 'false' },
          },
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['elasticloadbalancing:AddTags', 'elasticloadbalancing:RemoveTags'],
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
            'elasticloadbalancing:ModifyRule', 'elasticloadbalancing:SetIpAddressType',
            'elasticloadbalancing:SetSecurityGroups', 'elasticloadbalancing:SetSubnets',
            'elasticloadbalancing:DeleteLoadBalancer', 'elasticloadbalancing:DeleteTargetGroup',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['elasticloadbalancing:RegisterTargets', 'elasticloadbalancing:DeregisterTargets'],
          resources: ['arn:aws:elasticloadbalancing:*:*:targetgroup/*/*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'elasticloadbalancing:SetWebAcl', 'elasticloadbalancing:ModifyListener',
            'elasticloadbalancing:AddListenerCertificates', 'elasticloadbalancing:RemoveListenerCertificates',
            'elasticloadbalancing:ModifyRule',
            'elasticloadbalancing:ModifyListenerAttributes',
            'elasticloadbalancing:ModifyCapacityReservation',
            'elasticloadbalancing:ModifyIpPools',
            'elasticloadbalancing:SetRulePriorities',
          ],
          resources: ['*'],
        }),
      ],
    });
    role.addManagedPolicy(policy);
  }

  private addKarpenterPolicy(role: iam.Role, clusterName: string): void {
    const policy = new iam.ManagedPolicy(this, 'KarpenterPolicy', {
      managedPolicyName: `${clusterName}-karpenter-policy`,
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            'ec2:CreateFleet', 'ec2:CreateLaunchTemplate', 'ec2:CreateTags',
            'ec2:DescribeAvailabilityZones', 'ec2:DescribeImages', 'ec2:DescribeInstances',
            'ec2:DescribeInstanceTypeOfferings', 'ec2:DescribeInstanceTypes', 'ec2:DescribeLaunchTemplates',
            'ec2:DescribeSecurityGroups', 'ec2:DescribeSpotPriceHistory', 'ec2:DescribeSubnets',
            'ec2:DeleteLaunchTemplate', 'ec2:RunInstances', 'ec2:TerminateInstances',
            'iam:PassRole', 'iam:GetInstanceProfile', 'iam:CreateInstanceProfile',
            'iam:AddRoleToInstanceProfile', 'iam:DeleteInstanceProfile', 'iam:RemoveRoleFromInstanceProfile',
            'iam:TagInstanceProfile', 'ssm:GetParameter', 'pricing:GetProducts',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['eks:DescribeCluster'],
          resources: [this.cluster.clusterArn],
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ['sqs:DeleteMessage', 'sqs:GetQueueUrl', 'sqs:ReceiveMessage'],
          resources: [`arn:aws:sqs:${this.region}:${this.account}:${clusterName}-karpenter-interruption`],
        }),
      ],
    });
    role.addManagedPolicy(policy);
  }
}
