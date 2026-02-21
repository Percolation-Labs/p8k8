# P8 EKS Infrastructure (AWS CDK)

AWS CDK TypeScript infrastructure for P8 EKS clusters with proper namespace separation and Pod Identity.

## Architecture

### Namespace Architecture

| Namespace | Purpose | ServiceAccounts |
|-----------|---------|----------------|
| **p8** (configurable) | P8 application workloads (API, MCP, workers) | `p8-app` |
| **observability** | OpenTelemetry collector | `otel-collector` |
| **postgres-cluster** | CloudNativePG databases | `postgres-backup` |
| **karpenter** | Node autoscaling | `karpenter` |
| **kube-system** | Kubernetes system components | `ebs-csi-controller-sa`, `aws-load-balancer-controller` |
| **external-secrets-system** | Secret management | `external-secrets` |

### Key Design Decisions

1. **Single Application ServiceAccount**: All P8 workloads use the `p8-app` ServiceAccount
2. **Pod Identity (not IRSA)**: Using AWS EKS Pod Identity for simpler configuration
3. **Configurable App Namespace**: Defaults to `p8`, configurable via `APP_NAMESPACE`
4. **Split Stack Architecture**: Cluster and addons in separate stacks for resilience

## Configuration

All configuration via environment variables or `.env` file:

```bash
# AWS Configuration
AWS_ACCOUNT_ID=YOUR_ACCOUNT_ID
AWS_REGION=us-east-1
AWS_PROFILE=p8

# Cluster Configuration
CLUSTER_NAME_PREFIX=p8
ENVIRONMENT=dev
KUBERNETES_VERSION=1.34

# Feature Flags
ENABLE_KARPENTER=true
ENABLE_ALB_CONTROLLER=true
ENABLE_EXTERNAL_SECRETS=true
ENABLE_SSM_PARAMETERS=true

# SSM Parameters
SSM_PREFIX=/p8
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...
```

## Deployment

### Prerequisites
```bash
npm install
export AWS_PROFILE=p8
export AWS_REGION=us-east-1
```

### Bootstrap CDK (first time only)
```bash
npx cdk bootstrap aws://YOUR_ACCOUNT/us-east-1
```

### Deploy
```bash
# Deploy split stack (recommended)
npx cdk deploy P8EksClusterB --profile p8 --require-approval never 2>&1 | tee deploy.log
npx cdk deploy P8EksAddonsB --profile p8 --require-approval never 2>&1 | tee -a deploy.log

# Or deploy monolithic stack
npx cdk deploy P8ApplicationClusterA --profile p8 --require-approval never 2>&1 | tee deploy.log
```

### Connect to Cluster
```bash
aws eks update-kubeconfig --name p8-cluster-b --region us-east-1 --profile p8
kubectl get nodes
```

## Project Structure

```
cdk-eks/
├── bin/
│   └── cdk.ts                    # CDK app entry point
├── lib/
│   ├── config.ts                 # Configuration loading and validation
│   ├── shared-resources-stack.ts # ECR, shared IAM roles
│   ├── management-cluster-stack.ts # Management cluster (ArgoCD)
│   ├── eks-cluster-stack.ts      # Split: base cluster infrastructure
│   ├── eks-addons-stack.ts       # Split: K8s addons (storage, Helm)
│   └── worker-cluster-stack.ts   # Monolithic application cluster
├── .env.example                  # Environment configuration template
├── cdk.json                      # CDK configuration
├── package.json                  # Node.js dependencies
└── tsconfig.json                 # TypeScript configuration
```

## References

- [AWS EKS Best Practices](https://aws.github.io/aws-eks-best-practices/)
- [Karpenter Documentation](https://karpenter.sh/)
- [CloudNativePG Documentation](https://cloudnative-pg.io/)
- [AWS CDK Documentation](https://docs.aws.amazon.com/cdk/v2/guide/home.html)
- [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)
