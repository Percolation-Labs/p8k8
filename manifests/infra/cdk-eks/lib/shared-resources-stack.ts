import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { ClusterConfig } from './config';

export interface SharedResourcesStackProps extends cdk.StackProps {
  config: ClusterConfig;
}

export class SharedResourcesStack extends cdk.Stack {
  public readonly apiRepository: ecr.Repository;
  public readonly manifestsRepository: ecr.Repository;
  public readonly helmChartsRepository: ecr.Repository;
  public readonly githubActionsRole: iam.Role;

  constructor(scope: Construct, id: string, props: SharedResourcesStackProps) {
    super(scope, id, props);

    const prefix = props.config.clusterNamePrefix;

    // ECR Repository for application Docker images
    this.apiRepository = new ecr.Repository(this, 'ApiRepository', {
      repositoryName: `${prefix}/api`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.MUTABLE,
      lifecycleRules: [
        {
          description: 'Keep last 10 untagged images',
          maxImageCount: 10,
          tagStatus: ecr.TagStatus.UNTAGGED,
        },
        {
          description: 'Keep last 30 dev/staging images',
          maxImageCount: 30,
          tagPrefixList: ['dev', 'staging', 'pr'],
        },
        {
          description: 'Keep production images for 1 year',
          maxImageAge: cdk.Duration.days(365),
          tagPrefixList: ['prod', 'v'],
          rulePriority: 1,
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN, // Protect production images
    });

    // ECR Repository for manifests (optional - for GitOps image updates)
    this.manifestsRepository = new ecr.Repository(this, 'ManifestsRepository', {
      repositoryName: `${prefix}/manifests`,
      imageTagMutability: ecr.TagMutability.MUTABLE,
      lifecycleRules: [
        {
          maxImageCount: 10,
          tagStatus: ecr.TagStatus.UNTAGGED,
        },
      ],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ECR Repository for Helm charts as OCI artifacts
    // This stores packaged Helm charts for ArgoCD to deploy from
    this.helmChartsRepository = new ecr.Repository(this, 'HelmChartsRepository', {
      repositoryName: `${prefix}/helm-charts`,
      imageTagMutability: ecr.TagMutability.IMMUTABLE, // Semantic versioning immutability
      imageScanOnPush: true,
      lifecycleRules: [
        {
          description: 'Keep last 5 untagged charts',
          maxImageCount: 5,
          tagStatus: ecr.TagStatus.UNTAGGED,
        },
        {
          description: 'Keep last 20 development charts',
          maxImageCount: 20,
          tagPrefixList: ['0.', 'dev-', 'test-'],
        },
        {
          description: 'Keep production charts (1.x, 2.x) for 2 years',
          maxImageAge: cdk.Duration.days(730),
          tagPrefixList: ['1.', '2.', '3.', '4.', '5.'],
          rulePriority: 1,
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN, // Protect versioned charts
    });

    // GitHub OIDC Provider for GitHub Actions
    const githubOidcProvider = new iam.OpenIdConnectProvider(this, 'GitHubOIDC', {
      url: 'https://token.actions.githubusercontent.com',
      clientIds: ['sts.amazonaws.com'],
      // GitHub's thumbprint (valid as of 2025)
      thumbprints: ['6938fd4d98bab03faadb97b34396831e3780aea1'],
    });

    // IAM Role for GitHub Actions
    this.githubActionsRole = new iam.Role(this, 'GitHubActionsRole', {
      roleName: `${prefix}-github-actions`,
      description: `Role for GitHub Actions to deploy to ${prefix} ECR and EKS`,
      assumedBy: new iam.WebIdentityPrincipal(
        githubOidcProvider.openIdConnectProviderArn,
        {
          StringEquals: {
            'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
          },
          StringLike: {
            // Update with your GitHub org/repo
            'token.actions.githubusercontent.com:sub': 'repo:your-org/your-repo:*',
          },
        }
      ),
      maxSessionDuration: cdk.Duration.hours(1),
    });

    // Grant ECR push permissions
    this.apiRepository.grantPullPush(this.githubActionsRole);
    this.manifestsRepository.grantPullPush(this.githubActionsRole);
    this.helmChartsRepository.grantPullPush(this.githubActionsRole);

    // Grant CDK deploy permissions (for infrastructure updates)
    this.githubActionsRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('PowerUserAccess')
    );

    // Outputs
    new cdk.CfnOutput(this, 'ApiRepositoryUri', {
      value: this.apiRepository.repositoryUri,
      description: 'ECR repository URI for application Docker images',
      exportName: `${prefix}-api-repo-uri`,
    });

    new cdk.CfnOutput(this, 'HelmChartsRepositoryUri', {
      value: this.helmChartsRepository.repositoryUri,
      description: 'ECR repository URI for Helm charts (OCI registry)',
      exportName: `${prefix}-helm-charts-repo-uri`,
    });

    new cdk.CfnOutput(this, 'GitHubActionsRoleArn', {
      value: this.githubActionsRole.roleArn,
      description: 'IAM role ARN for GitHub Actions',
      exportName: `${prefix}-github-actions-role-arn`,
    });
  }
}
