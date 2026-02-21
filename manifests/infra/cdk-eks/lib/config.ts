import * as cdk from 'aws-cdk-lib';
import * as dotenv from 'dotenv';
import * as path from 'path';

// Load .env file if it exists
dotenv.config({ path: path.join(__dirname, '../.env') });

export type DeploymentMode = 'minimal' | 'standard' | 'full';

export interface ClusterConfig {
  // AWS Configuration
  account: string;
  region: string;
  profile?: string;

  // Cluster Configuration
  clusterNamePrefix: string;
  environment: string;
  deploymentMode: DeploymentMode;
  kubernetesVersion: string;
  appNamespace: string; // Application namespace for P8 workloads (default: 'p8')

  // VPC Configuration
  existingVpcId?: string;
  vpcName?: string;
  privateSubnetNames: string[];
  publicSubnetNames: string[];

  // GitHub Configuration
  githubOrg: string;
  githubRepo: string;
  githubOidcUrl: string;
  githubOidcThumbprint: string;

  // S3 Configuration
  appBucketName: string;

  // ECR Configuration
  ecrApiRepo: string;
  ecrManifestsRepo: string;

  // Existing Resources
  existingEksCluster?: string;
  existingIamRoleBackend?: string;
  existingIamRoleSecrets?: string;
  iamRolePath: string;

  // Admin Access
  adminRoleArn?: string; // IAM role/user ARN for kubectl admin access

  // Feature Flags
  enableKarpenter: boolean;
  enableAlbController: boolean;
  enableExternalSecrets: boolean;
  enableCertManager: boolean;
  enableAdot: boolean;
  enablePodIdentity: boolean;
  enableArgoCD: boolean;
  enableSsmParameters: boolean;

  // SSM Configuration
  ssmPrefix: string;

  // API Keys (from environment)
  anthropicApiKey: string;
  openaiApiKey: string;
  googleClientId: string;
  googleClientSecret: string;

  // Helm Chart Versions
  argoCDVersion: string;

  // Cost Optimization
  useSpotInstances: boolean;
  mgmtInstanceType: string;
  mgmtMinSize: number;
  mgmtMaxSize: number;
  workerInstanceType: string;
  workerMinSize: number;
  workerMaxSize: number;
}

export function loadConfig(): ClusterConfig {
  const deploymentMode = (process.env.DEPLOYMENT_MODE || 'minimal') as DeploymentMode;

  const config: ClusterConfig = {
    // AWS Configuration
    account: process.env.AWS_ACCOUNT_ID || process.env.CDK_DEFAULT_ACCOUNT || '',
    region: process.env.AWS_REGION || process.env.CDK_DEFAULT_REGION || 'eu-west-1',
    profile: process.env.AWS_PROFILE,

    // Cluster Configuration
    clusterNamePrefix: process.env.CLUSTER_NAME_PREFIX || 'p8',
    environment: process.env.ENVIRONMENT || 'dev',
    deploymentMode,
    kubernetesVersion: process.env.KUBERNETES_VERSION || '1.34',
    appNamespace: process.env.APP_NAMESPACE || process.env.CLUSTER_NAME_PREFIX || 'p8',

    // VPC Configuration
    existingVpcId: process.env.EXISTING_VPC_ID,
    vpcName: process.env.VPC_NAME,
    privateSubnetNames: (process.env.PRIVATE_SUBNET_NAMES || 'A Private,B Private,C Private').split(','),
    publicSubnetNames: (process.env.PUBLIC_SUBNET_NAMES || 'A Public,B Public,C Public').split(','),

    // GitHub Configuration
    githubOrg: process.env.GITHUB_ORG || 'Percolation-Labs',
    githubRepo: process.env.GITHUB_REPO || 'p8k8',
    githubOidcUrl: process.env.GITHUB_OIDC_URL || 'https://token.actions.githubusercontent.com',
    githubOidcThumbprint: process.env.GITHUB_OIDC_THUMBPRINT || '6938fd4d98bab03faadb97b34396831e3780aea1',

    // S3 Configuration
    // Default: cluster-specific bucket name, e.g., "p8-management-dev-data"
    appBucketName: process.env.APP_BUCKET_NAME || '',

    // ECR Configuration
    ecrApiRepo: process.env.ECR_API_REPO || 'p8/api',
    ecrManifestsRepo: process.env.ECR_MANIFESTS_REPO || 'p8/manifests',

    // Existing Resources
    existingEksCluster: process.env.EXISTING_EKS_CLUSTER,
    existingIamRoleBackend: process.env.EXISTING_IAM_ROLE_BACKEND,
    existingIamRoleSecrets: process.env.EXISTING_IAM_ROLE_SECRETS,
    iamRolePath: process.env.IAM_ROLE_PATH || '/',

    // Admin Access
    adminRoleArn: process.env.ADMIN_ROLE_ARN,

    // Feature Flags
    enableKarpenter: process.env.ENABLE_KARPENTER !== 'false',
    enableAlbController: process.env.ENABLE_ALB_CONTROLLER !== 'false',
    enableExternalSecrets: process.env.ENABLE_EXTERNAL_SECRETS !== 'false',
    enableCertManager: process.env.ENABLE_CERT_MANAGER !== 'false',
    enableAdot: process.env.ENABLE_ADOT !== 'false',
    enablePodIdentity: process.env.ENABLE_POD_IDENTITY !== 'false',
    enableArgoCD: process.env.ENABLE_ARGOCD !== 'false', // Set to 'false' if ArgoCD on management cluster
    enableSsmParameters: process.env.ENABLE_SSM_PARAMETERS !== 'false', // Create SSM params for secrets

    // SSM Configuration
    ssmPrefix: process.env.SSM_PREFIX || '/p8',

    // API Keys (from environment) - required if enableSsmParameters is true
    anthropicApiKey: process.env.ANTHROPIC_API_KEY || '',
    openaiApiKey: process.env.OPENAI_API_KEY || '',
    googleClientId: process.env.GOOGLE_CLIENT_ID || 'placeholder',
    googleClientSecret: process.env.GOOGLE_CLIENT_SECRET || 'placeholder',

    // Helm Chart Versions
    argoCDVersion: process.env.ARGOCD_VERSION || '7.7.5', // Stable ArgoCD chart version

    // Cost Optimization
    useSpotInstances: process.env.USE_SPOT_INSTANCES === 'true',
    mgmtInstanceType: process.env.MGMT_INSTANCE_TYPE || 't3.small',
    mgmtMinSize: parseInt(process.env.MGMT_MIN_SIZE || '2'),
    mgmtMaxSize: parseInt(process.env.MGMT_MAX_SIZE || '3'),
    workerInstanceType: process.env.WORKER_INSTANCE_TYPE || 't3.medium',
    workerMinSize: parseInt(process.env.WORKER_MIN_SIZE || '2'),
    workerMaxSize: parseInt(process.env.WORKER_MAX_SIZE || '3'),
  };

  // Validate configuration
  if (!config.account) {
    throw new Error('AWS_ACCOUNT_ID or CDK_DEFAULT_ACCOUNT must be set');
  }

  if (!config.region) {
    throw new Error('AWS_REGION or CDK_DEFAULT_REGION must be set');
  }

  return config;
}

export function getStackName(baseName: string, config: ClusterConfig): string {
  return `${config.clusterNamePrefix}-${baseName}-${config.environment}`;
}

export function shouldDeployWorkerCluster(config: ClusterConfig): boolean {
  return config.deploymentMode === 'standard' || config.deploymentMode === 'full';
}

export function shouldDeployProductionCluster(config: ClusterConfig): boolean {
  return config.deploymentMode === 'full';
}

export function getBucketName(baseName: string, config: ClusterConfig): string {
  // If explicit bucket name provided, use it
  if (config.appBucketName) {
    return config.appBucketName;
  }
  // Otherwise, generate cluster-specific bucket name
  return `${config.clusterNamePrefix}-${baseName}-${config.environment}-data`;
}

/**
 * Get compatible EKS addon versions for a given Kubernetes version
 * Based on AWS EKS documentation and compatibility matrix
 */
export function getAddonVersions(k8sVersion: string): {
  vpcCni: string;
  coreDns: string;
  kubeProxy: string;
  ebsCsi: string;
  podIdentityAgent: string;
} {
  // Map K8s versions to their compatible addon versions
  const versionMap: Record<string, any> = {
    '1.34': {
      vpcCni: 'v1.20.4-eksbuild.2',
      coreDns: 'v1.12.1-eksbuild.2',
      kubeProxy: 'v1.34.0-eksbuild.2',
      ebsCsi: 'v1.52.1-eksbuild.1', // EBS CSI is forward-compatible across K8s versions
      podIdentityAgent: 'v1.3.9-eksbuild.5', // Pod Identity agent compatible with K8s 1.34
    },
    '1.33': {
      vpcCni: 'v1.20.5-eksbuild.1',
      coreDns: 'v1.12.4-eksbuild.1',
      kubeProxy: 'v1.33.5-eksbuild.2',
      ebsCsi: 'v1.53.0-eksbuild.1',
      podIdentityAgent: 'v1.3.10-eksbuild.1',
    },
    '1.32': {
      vpcCni: 'v1.20.4-eksbuild.2',
      coreDns: 'v1.11.4-eksbuild.24',
      kubeProxy: 'v1.32.0-eksbuild.2',
      ebsCsi: 'v1.52.1-eksbuild.1',
      podIdentityAgent: 'v1.3.9-eksbuild.5',
    },
    '1.31': {
      vpcCni: 'v1.20.4-eksbuild.2',
      coreDns: 'v1.11.4-eksbuild.24',
      kubeProxy: 'v1.31.10-eksbuild.12',
      ebsCsi: 'v1.52.1-eksbuild.1',
      podIdentityAgent: 'v1.4.0-eksbuild.1',
    },
  };

  const versions = versionMap[k8sVersion];
  if (!versions) {
    console.warn(`Warning: No addon version mapping for K8s ${k8sVersion}, falling back to 1.31 versions`);
    return versionMap['1.31'];
  }

  return versions;
}

export function printConfig(config: ClusterConfig): void {
  console.log('\nDeployment Configuration:');
  console.log('================================');
  console.log(`Account: ${config.account}`);
  console.log(`Region: ${config.region}`);
  console.log(`Profile: ${config.profile || 'default'}`);
  console.log(`Environment: ${config.environment}`);
  console.log(`Deployment Mode: ${config.deploymentMode}`);
  console.log(`Kubernetes Version: ${config.kubernetesVersion}`);
  console.log(`\nResource Strategy: Bootstrap from Zero`);
  console.log(`  - VPC: ${config.existingVpcId || 'Create New'}`);
  console.log(`  - S3 Bucket: ${config.appBucketName || 'Cluster-specific (auto-generated)'}`);
  console.log(`  - IAM Roles: ${config.existingIamRoleBackend ? 'Use Existing' : 'Create New'}`);
  console.log(`\nFeatures:`);
  console.log(`  - Karpenter: ${config.enableKarpenter}`);
  console.log(`  - ALB Controller: ${config.enableAlbController}`);
  console.log(`  - ArgoCD: ${config.enableArgoCD} (v${config.argoCDVersion})`);
  console.log(`  - External Secrets: ${config.enableExternalSecrets}`);
  console.log(`  - Cert Manager: ${config.enableCertManager}`);
  console.log(`  - ADOT: ${config.enableAdot}`);
  console.log(`  - Pod Identity: ${config.enablePodIdentity}`);
  console.log(`  - SSM Parameters: ${config.enableSsmParameters} (prefix: ${config.ssmPrefix})`);
  if (config.enableSsmParameters) {
    console.log(`    - ANTHROPIC_API_KEY: ${config.anthropicApiKey ? 'set' : 'missing'}`);
    console.log(`    - OPENAI_API_KEY: ${config.openaiApiKey ? 'set' : 'missing'}`);
  }
  console.log(`\nCost Optimization:`);
  console.log(`  - Spot Instances: ${config.useSpotInstances}`);
  console.log(`  - Mgmt Instance Type: ${config.mgmtInstanceType}`);
  console.log(`  - Worker Instance Type: ${config.workerInstanceType}`);
  console.log('================================\n');
}
