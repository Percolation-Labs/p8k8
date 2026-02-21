#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { SharedResourcesStack } from '../lib/shared-resources-stack';
import { ManagementClusterStack } from '../lib/management-cluster-stack';
import { WorkerClusterStack } from '../lib/worker-cluster-stack';
import { EksClusterStack } from '../lib/eks-cluster-stack';
import { EksAddonsStack } from '../lib/eks-addons-stack';
import { loadConfig, printConfig } from '../lib/config';

const app = new cdk.App();

// Load configuration from environment variables and .env file
const config = loadConfig();
printConfig(config);

// AWS Account and Region
// IMPORTANT: Set AWS_REGION environment variable to deploy to specific region
// Example: export AWS_REGION=eu-central-1
// Available EU regions with EIP quota: eu-central-1, eu-west-2, eu-north-1
// Default: eu-west-1 (has limited EIP quota - 4/5 used)
const env = {
  account: config.account,
  region: config.region,
};

// Stack name prefix based on cluster name prefix (e.g., "p8" -> "P8")
const stackPrefix = config.clusterNamePrefix.toUpperCase();

// Shared resources (ECR, IAM roles, etc.)
const sharedStack = new SharedResourcesStack(app, `${stackPrefix}SharedResources`, {
  env,
  description: `Shared resources for ${config.clusterNamePrefix} EKS clusters (ECR, IAM)`,
  config,
});

// Management cluster for ArgoCD and GitOps
// Note: Uses 1 NAT gateway by default for testing (saves $64/month + 2 EIPs)
// For production HA, pass natGateways: 3
const mgmtStack = new ManagementClusterStack(app, `${stackPrefix}ManagementCluster`, {
  env,
  description: `Management cluster with ArgoCD for ${config.clusterNamePrefix} GitOps`,
  ecrRepository: sharedStack.apiRepository,
  config,
  // natGateways: 1, // Default, can set to 3 for production HA
});

// Application cluster A (monolithic stack - original)
const appClusterA = new WorkerClusterStack(app, `${stackPrefix}ApplicationClusterA`, {
  env,
  description: `Application cluster for ${config.clusterNamePrefix} ${config.environment} workloads`,
  clusterName: `${config.clusterNamePrefix}-application-cluster-a`,
  environment: config.environment,
  ecrRepository: sharedStack.apiRepository,
  config,
});

// ============================================================
// SPLIT STACK ARCHITECTURE (Resilient - use for new deployments)
// ============================================================
// These stacks are separate so if addons fail, cluster survives:
// - Deploy cluster first: cdk deploy P8EksClusterB
// - Deploy addons after:  cdk deploy P8EksAddonsB
//
// Benefits:
// - If addons fail (e.g., rate limiting), cluster stays intact
// - Can retry addons without recreating 20-minute cluster
// - Faster iteration on K8s manifest changes
// ============================================================

const clusterB = new EksClusterStack(app, `${stackPrefix}EksClusterB`, {
  env,
  description: `EKS cluster infrastructure for ${config.clusterNamePrefix} ${config.environment}`,
  clusterName: `${config.clusterNamePrefix}-cluster-b`,
  environment: config.environment,
  ecrRepository: sharedStack.apiRepository,
  config,
});

const addonsB = new EksAddonsStack(app, `${stackPrefix}EksAddonsB`, {
  env,
  description: `K8s addons for ${config.clusterNamePrefix} ${config.environment} (storage, namespaces, Karpenter)`,
  clusterStack: clusterB,
  config,
  environment: config.environment,
  clusterName: `${config.clusterNamePrefix}-cluster-b`,
});

// Ensure addons deploy after cluster
addonsB.addDependency(clusterB);

// Uncomment to create production worker cluster
// const workerStackB = new WorkerClusterStack(app, 'P8WorkerClusterB', {
//   env,
//   description: 'Worker cluster for production workloads',
//   clusterName: 'worker-cluster-b',
//   environment: 'production',
//   ecrRepository: sharedStack.apiRepository,
//   config,
// });

// Tags for all resources
cdk.Tags.of(app).add('Project', config.clusterNamePrefix);
cdk.Tags.of(app).add('ManagedBy', 'CDK');
cdk.Tags.of(app).add('Environment', config.environment);

app.synth();
