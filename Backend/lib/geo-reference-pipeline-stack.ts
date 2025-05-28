import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { AwsCustomResource, AwsCustomResourcePolicy, PhysicalResourceId } from 'aws-cdk-lib/custom-resources';

export class GeoReferencePipelineStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // CloudFormation parameters
    const bucketNameParam = new cdk.CfnParameter(this, 'BucketName', {
      type: 'String',
      description: 'S3 bucket name for input/output files. Must be globally unique.',
    });

    const errorFolderParam = new cdk.CfnParameter(this, 'ErrorFolder', {
      type: 'String',
      description: 'S3 folder for error logs.',
      default: 'error',
    });

    const analysisFolderParam = new cdk.CfnParameter(this, 'AnalysisFolder', {
      type: 'String',
      description: 'S3 folder for analysis outputs.',
      default: 'analysis',
    });

    const githubTokenParam = new cdk.CfnParameter(this, 'GithubToken', {
      type: 'String',
      description: 'GitHub token for repository access.',
      noEcho: true,
    });

    const githubRepoNameParam = new cdk.CfnParameter(this, 'GithubRepoName', {
      type: 'String',
      description: 'GitHub repository name for GeoJSON uploads.',
    });

    const bedrockModelIdParam = new cdk.CfnParameter(this, 'BedrockModelId', {
      type: 'String',
      description: 'AWS Bedrock model ID.',
      default: 'us.anthropic.claude-3-7-sonnet-20250219-v1:0',
    });

    const bedrockRegionParam = new cdk.CfnParameter(this, 'BedrockRegion', {
      type: 'String',
      description: 'AWS region for Bedrock service.',
      default: 'us-west-2',
    });

    // Parameter values
    const bucketName = bucketNameParam.valueAsString;
    const errorFolder = errorFolderParam.valueAsString;
    const analysisFolder = analysisFolderParam.valueAsString;
    const githubToken = githubTokenParam.valueAsString;
    const githubRepoName = githubRepoNameParam.valueAsString;
    const bedrockModelId = bedrockModelIdParam.valueAsString;
    const bedrockRegion = bedrockRegionParam.valueAsString;

    // S3 bucket
    const bucket = new s3.Bucket(this, 'GeoPipelineBucket', {
      bucketName: bucketName,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    // Create S3 prefixes using AwsCustomResource
    const prefixes = ['raw_maps/', `${errorFolder}/`, `${analysisFolder}/`];
    prefixes.forEach((prefix, index) => {
      new AwsCustomResource(this, `CreateS3Prefix${index}`, {
        onCreate: {
          service: 'S3',
          action: 'putObject',
          parameters: {
            Bucket: bucket.bucketName,
            Key: prefix,
          },
          physicalResourceId: PhysicalResourceId.of(`S3Prefix-${prefix}`),
        },
        onUpdate: {
          service: 'S3',
          action: 'putObject',
          parameters: {
            Bucket: bucket.bucketName,
            Key: prefix,
          },
          physicalResourceId: PhysicalResourceId.of(`S3Prefix-${prefix}`),
        },
        policy: AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['s3:PutObject'],
            resources: [`${bucket.bucketArn}/*`],
          }),
        ]),
      });
    });

    // Lambda layer
    const pillowLayer = new lambda.LayerVersion(this, 'PillowLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../layers/compress_image_pillow.zip')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_13],
      description: 'Pillow dependencies for image compression',
    });

    const geoJsonLayer = new lambda.LayerVersion(this, 'GeoJsonLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '../layers/createTheGeoJsonLayer.zip')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_13],
      description: 'Dependencies for GeoJSON processing',
    });

    // Lambda function
    const analysisLambda = new lambda.Function(this, 'GeoAnalysisLambda', {
      functionName: 'GeoAnalysisLambda',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda')),
      layers: [pillowLayer, geoJsonLayer],
      timeout: cdk.Duration.minutes(15),
      memorySize: 10240,
      ephemeralStorageSize: cdk.Size.mebibytes(10240),
      environment: {
        BUCKET_NAME: bucketName,
        ERROR_FOLDER: errorFolder,
        ANALYSIS_FOLDER: analysisFolder,
        GITHUB_TOKEN: githubToken,
        GITHUB_REPO_NAME: githubRepoName,
        BEDROCK_MODEL_ID: bedrockModelId,
        BEDROCK_REGION: bedrockRegion,
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    // Lambda permissions
    bucket.grantReadWrite(analysisLambda);
    analysisLambda.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['textract:DetectDocumentText', 'bedrock:InvokeModel'],
        resources: ['*'],
      })
    );
    analysisLambda.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: ['*'],
      })
    );

    bucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(analysisLambda),
      { prefix: 'raw_maps/', suffix: '.tif' }
    );

    // Outputs
    new cdk.CfnOutput(this, 'BucketNameOutput', {
      value: bucket.bucketName,
      description: 'S3 bucket for Geo Pipeline',
    });
    new cdk.CfnOutput(this, 'LambdaFunctionArn', {
      value: analysisLambda.functionArn,
      description: 'ARN of the GeoAnalysis Lambda function',
    });
    new cdk.CfnOutput(this, 'UploadInstruction', {
      value: `Upload images to s3://${bucket.bucketName}/raw_maps/ with extensions: '.tif'}`,
      description: 'Instructions for using the S3 bucket',
    });
  }
}