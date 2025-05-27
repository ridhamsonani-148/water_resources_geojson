#!/usr/bin/env bash
set -euo pipefail

# Prompt for required inputs
if [ -z "${BUCKET_NAME:-}" ]; then
  read -rp "Enter S3 bucket name (must be globally unique): " BUCKET_NAME
fi

if [ -z "${ERROR_FOLDER:-}" ]; then
  read -rp "Enter S3 error folder [default: error]: " ERROR_FOLDER
  ERROR_FOLDER=${ERROR_FOLDER:-error}
fi

if [ -z "${ANALYSIS_FOLDER:-}" ]; then
  read -rp "Enter S3 analysis folder [default: analysis]: " ANALYSIS_FOLDER
  ANALYSIS_FOLDER=${ANALYSIS_FOLDER:-analysis}
fi

if [ -z "${GITHUB_TOKEN:-}" ]; then
  read -rp "Enter GitHub token: " GITHUB_TOKEN
fi

if [ -z "${GITHUB_URL:-}" ]; then
  read -rp "Enter GitHub repository URL (e.g., https://github.com/OWNER/REPO): " GITHUB_URL
fi

# Normalize and parse GitHub URL
clean_url=${GITHUB_URL%.git}
clean_url=${clean_url%/}
if [[ $clean_url =~ ^https://github\.com/([^/]+/[^/]+)$ ]]; then
  path="${BASH_REMATCH[1]}"
elif [[ $clean_url =~ ^git@github\.com:([^/]+/[^/]+)$ ]]; then
  path="${BASH_REMATCH[1]}"
else
  echo "Unable to parse repo from '$GITHUB_URL'"
  read -rp "Enter GitHub repo name manually: " GITHUB_REPO_NAME
fi

if [ -z "${GITHUB_REPO_NAME:-}" ]; then
  GITHUB_REPO_NAME=${path##*/}
  echo "Detected GitHub Repo: $GITHUB_REPO_NAME"
  read -rp "Is this correct? (y/n): " CONFIRM
  CONFIRM=$(printf '%s' "$CONFIRM" | tr '[:upper:]' '[:lower:]')
  if [[ "$CONFIRM" != "y" && "$CONFIRM" != "yes" ]]; then
    read -rp "Enter GitHub repo name manually: " GITHUB_REPO_NAME
  fi
fi

if [ -z "${BEDROCK_MODEL_ID:-}" ]; then
  read -rp "Enter Bedrock model ID [default: us.anthropic.claude-3-7-sonnet-20250219-v1:0]: " BEDROCK_MODEL_ID
  BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-us.anthropic.claude-3-7-sonnet-20250219-v1:0}
fi

if [ -z "${BEDROCK_REGION:-}" ]; then
  read -rp "Enter Bedrock region [default: us-west-2]: " BEDROCK_REGION
  BEDROCK_REGION=${BEDROCK_REGION:-us-west-2}
fi

if [ -z "${ACTION:-}" ]; then
  read -rp "Enter action [deploy/destroy]: " ACTION
  ACTION=$(printf '%s' "$ACTION" | tr '[:upper:]' '[:lower:]')
fi

if [[ "$ACTION" != "deploy" && "$ACTION" != "destroy" ]]; then
  echo "Invalid action: '$ACTION'. Choose 'deploy' or 'destroy'."
  exit 1
fi

# Install dependencies
echo "Installing dependencies..."
npm install
pip install -r requirements.txt

# Build TypeScript
echo "Building TypeScript sources..."
npm run build

# Bootstrap CDK
echo "Bootstrapping CDK..."
cdk bootstrap --require-approval never

# Deploy or destroy
if [ "$ACTION" = "destroy" ]; then
  echo "Destroying CDK stack..."
  cdk destroy GeoReferencePipelineStack --force \
    --parameters BucketName="$BUCKET_NAME" \
    --parameters ErrorFolder="$ERROR_FOLDER" \
    --parameters AnalysisFolder="$ANALYSIS_FOLDER" \
    --parameters GithubToken="$GITHUB_TOKEN" \
    --parameters GithubRepoName="$GITHUB_REPO_NAME" \
    --parameters BedrockModelId="$BEDROCK_MODEL_ID" \
    --parameters BedrockRegion="$BEDROCK_REGION"
else
  echo "Deploying CDK stack..."
  cdk deploy GeoReferencePipelineStack --require-approval never \
    --parameters BucketName="$BUCKET_NAME" \
    --parameters ErrorFolder="$ERROR_FOLDER" \
    --parameters AnalysisFolder="$ANALYSIS_FOLDER" \
    --parameters GithubToken="$GITHUB_TOKEN" \
    --parameters GithubRepoName="$GITHUB_REPO_NAME" \
    --parameters BedrockModelId="$BEDROCK_MODEL_ID" \
    --parameters BedrockRegion="$BEDROCK_REGION"
fi

echo "Action '$ACTION' completed."
exit 0