#!/usr/bin/env bash
set -euo pipefail

# Prompt for GitHub URL
if [ -z "${GITHUB_URL:-}" ]; then
  read -rp "Enter source GitHub repository URL (e.g., https://github.com/OWNER/REPO): " GITHUB_URL
fi

# Normalize URL
clean_url=${GITHUB_URL%.git}
clean_url=${clean_url%/}

# Extract owner/repo
if [[ $clean_url =~ ^https://github\.com/([^/]+/[^/]+)$ ]]; then
  path="${BASH_REMATCH[1]}"
elif [[ $clean_url =~ ^git@github\.com:([^/]+/[^/]+)$ ]]; then
  path="${BASH_REMATCH[1]}"
else
  echo "Unable to parse owner/repo from '$GITHUB_URL'"
  read -rp "Enter GitHub owner: " GITHUB_OWNER
  read -rp "Enter GitHub repo: " GITHUB_REPO
fi

if [ -z "${GITHUB_OWNER:-}" ] || [ -z "${GITHUB_REPO:-}" ]; then
  GITHUB_OWNER=${path%%/*}
  GITHUB_REPO=${path##*/}
  echo "Detected GitHub Owner: $GITHUB_OWNER"
  echo "Detected GitHub Repo: $GITHUB_REPO"
  read -rp "Is this correct? (y/n): " CONFIRM
  CONFIRM=$(printf '%s' "$CONFIRM" | tr '[:upper:]' '[:lower:]')
  if [[ "$CONFIRM" != "y" && "$CONFIRM" != "yes" ]]; then
    read -rp "Enter GitHub owner: " GITHUB_OWNER
    read -rp "Enter GitHub repo: " GITHUB_REPO
  fi
fi

# Prompt for client’s private GitHub repository URL
if [ -z "${CLIENT_GITHUB_URL:-}" ]; then
  read -rp "Enter client’s private GitHub repository URL (e.g., https://github.com/client-org/water-resources-archive): " CLIENT_GITHUB_URL
fi

# Normalize client URL
client_clean_url=${CLIENT_GITHUB_URL%.git}
client_clean_url=${client_clean_url%/}

# Extract owner/repo for client’s repository
if [[ $client_clean_url =~ ^https://github\.com/([^/]+/[^/]+)$ ]]; then
  client_path="${BASH_REMATCH[1]}"
elif [[ $client_clean_url =~ ^git@github\.com:([^/]+/[^/]+)$ ]]; then
  client_path="${BASH_REMATCH[1]}"
else
  echo "Unable to parse owner/repo from '$CLIENT_GITHUB_URL'"
  read -rp "Enter client’s GitHub owner: " CLIENT_GITHUB_OWNER
  read -rp "Enter client’s GitHub repo: " CLIENT_GITHUB_REPO_NAME
fi

if [ -z "${CLIENT_GITHUB_OWNER:-}" ] || [ -z "${CLIENT_GITHUB_REPO_NAME:-}" ]; then
  CLIENT_GITHUB_OWNER=${client_path%%/*}
  CLIENT_GITHUB_REPO_NAME=${client_path##*/}
  echo "Detected client’s GitHub Owner: $CLIENT_GITHUB_OWNER"
  echo "Detected client’s GitHub Repo: $CLIENT_GITHUB_REPO_NAME"
  read -rp "Is this correct? (y/n): " CONFIRM
  CONFIRM=$(printf '%s' "$CONFIRM" | tr '[:upper:]' '[:lower:]')
  if [[ "$CONFIRM" != "y" && "$CONFIRM" != "yes" ]]; then
    read -rp "Enter client’s GitHub owner: " CLIENT_GITHUB_OWNER
    read -rp "Enter client’s GitHub repo: " CLIENT_GITHUB_REPO_NAME
  fi
fi

# Prompt for client’s GitHub token
if [ -z "${CLIENT_GITHUB_TOKEN:-}" ]; then
  read -rp "Enter client’s GitHub token for private repository: " CLIENT_GITHUB_TOKEN
fi

# Prompt for other parameters
if [ -z "${PROJECT_NAME:-}" ]; then
  read -rp "Enter CodeBuild project name (e.g., GeoPipelineDeploy): " PROJECT_NAME
fi

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

# Validate client’s GitHub token and repository
echo "Validating client’s GitHub token and repository..."
repo_check=$(curl -s -H "Authorization: token $CLIENT_GITHUB_TOKEN" "https://api.github.com/repos/$CLIENT_GITHUB_OWNER/$CLIENT_GITHUB_REPO_NAME")
if echo "$repo_check" | grep -q "Not Found"; then
  echo "Error: Client repository $CLIENT_GITHUB_OWNER/$CLIENT_GITHUB_REPO_NAME not found or token invalid."
  exit 1
fi

# Create IAM role
ROLE_NAME="${PROJECT_NAME}-service-role"
echo "Checking for IAM role: $ROLE_NAME"

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "✓ IAM role exists"
  ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text)
else
  echo "✱ Creating IAM role: $ROLE_NAME"
  TRUST_DOC='{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"codebuild.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]
  }'

  ROLE_ARN=$(aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_DOC" \
    --query 'Role.Arn' --output text)

  echo "Attaching policies..."
  aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
  aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess

  echo "Waiting for IAM role to propagate..."
  sleep 10
fi

# Create CodeBuild project
echo "Creating CodeBuild project: $PROJECT_NAME"

ENVIRONMENT='{
  "type": "LINUX_CONTAINER",
  "image": "aws/codebuild/standard:7.0",
  "computeType": "BUILD_GENERAL1_SMALL",
  "environmentVariables": [
    {"name": "BUCKET_NAME", "value": "'"$BUCKET_NAME"'", "type": "PLAINTEXT"},
    {"name": "ERROR_FOLDER", "value": "'"$ERROR_FOLDER"'", "type": "PLAINTEXT"},
    {"name": "ANALYSIS_FOLDER", "value": "'"$ANALYSIS_FOLDER"'", "type": "PLAINTEXT"},
    {"name": "GITHUB_TOKEN", "value": "'"$CLIENT_GITHUB_TOKEN"'", "type": "PLAINTEXT"},
    {"name": "GITHUB_REPO", "value": "'"$CLIENT_GITHUB_REPO_NAME"'", "type": "PLAINTEXT"},
    {"name": "BEDROCK_MODEL_ID", "value": "'"$BEDROCK_MODEL_ID"'", "type": "PLAINTEXT"},
    {"name": "BEDROCK_REGION", "value": "'"$BEDROCK_REGION"'", "type": "PLAINTEXT"},
    {"name": "ACTION", "value": "'"$ACTION"'", "type": "PLAINTEXT"}
  ]
}'

ARTIFACTS='{"type":"NO_ARTIFACTS"}'
SOURCE='{
  "type":"GITHUB",
  "location":"'"$GITHUB_URL"'",
  "buildspec":"Backend/buildspec.yml"
}'

aws codebuild create-project \
  --name "$PROJECT_NAME" \
  --source "$SOURCE" \
  --artifacts "$ARTIFACTS" \
  --environment "$ENVIRONMENT" \
  --service-role "$ROLE_ARN" \
  --output json \
  --no-cli-pager

if [ $? -eq 0 ]; then
  echo "✓ CodeBuild project '$PROJECT_NAME' created."
else
  echo "✗ Failed to create CodeBuild project."
  exit 1
fi

# Start build
echo "Starting build for '$PROJECT_NAME'..."
aws codebuild start-build \
  --project-name "$PROJECT_NAME" \
  --no-cli-pager \
  --output json

if [ $? -eq 0 ]; then
  echo "✓ Build started."
else
  echo "✗ Failed to start build."
  exit 1
fi

echo "Current CodeBuild projects:"
aws codebuild list-projects --output table

exit 0