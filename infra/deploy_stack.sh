#!/usr/bin/env bash
#
# One command, nothing from nothing (PLAN Stage 10 acceptance).
# Creates/updates the whole stack: ECR, ECS, ALB, RDS+pgvector, autoscaling,
# both IAM roles. Idempotent — safe to re-run for an infra tweak without ever
# rolling the running image back to the bootstrap placeholder.
#
# Required env vars:
#   GITHUB_ORG        your GitHub username or org
#   GITHUB_REPO       the repo name this workflow lives in
#   ANTHROPIC_API_KEY a real key — stored into SSM, NOT into the template
#
# Optional:
#   AWS_REGION        defaults to ap-south-1
#   APP_NAME          defaults to ordercare-agent
#   LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
#                     placeholders are written if unset; tracing then no-ops.
#
# NOTE ON QUOTING: values go into SSM raw and are injected into the container
# raw — there is no dotenv layer to strip quotes. A key stored as "sk-ant-..."
# WITH quotes produces a 401 that looks like a bad key. Do not quote values in
# the shell beyond normal shell quoting.

set -euo pipefail

# Git Bash / MSYS on Windows rewrites any argument that looks like a Unix path
# into a Windows path, so an SSM name like "/ordercare-agent/DB_PASSWORD" arrives
# at the API as "C:/Program Files/Git/ordercare-agent/DB_PASSWORD" and is rejected
# with "Parameter name must be a fully qualified name." Harmless no-op on Linux
# and macOS, required on Windows.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

: "${GITHUB_ORG:?set GITHUB_ORG}"
: "${GITHUB_REPO:?set GITHUB_REPO}"
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
APP_NAME="${APP_NAME:-ordercare-agent}"
STACK_NAME="${APP_NAME}-infra"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "Account: $ACCOUNT_ID   Region: $AWS_REGION   App: $APP_NAME"

OIDC_ARN=$(bash "$SCRIPT_DIR/setup_oidc_provider.sh" | tail -1)
echo "OIDC provider: $OIDC_ARN"

# ---------------------------------------------------------------- SSM config
# Secrets live outside the stack on purpose: rotating a key should not trigger
# a stack update, and a stack update should not require re-supplying keys.

# The DB password is generated once and then reused, so re-running this script
# does not try to change the RDS master password on every deploy. Character set
# is deliberately alphanumeric — RDS rejects '/', '@', '"' and spaces.
if EXISTING_PW=$(aws ssm get-parameter --name "/${APP_NAME}/DB_PASSWORD" --with-decryption \
                   --region "$AWS_REGION" --query "Parameter.Value" --output text 2>/dev/null); then
  DB_PASSWORD="$EXISTING_PW"
  echo "Reusing existing DB password from SSM"
else
  # pipefail must be off for exactly this pipeline: `head -c 24` closes the pipe
  # as soon as it has its bytes, `tr` gets SIGPIPE reading from an infinite
  # /dev/urandom and exits 141, and pipefail+set -e then kills the script with no
  # error message at all. Restored immediately after.
  set +o pipefail
  DB_PASSWORD=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
  set -o pipefail
  aws ssm put-parameter --name "/${APP_NAME}/DB_PASSWORD" --value "$DB_PASSWORD" \
    --type SecureString --region "$AWS_REGION" --overwrite >/dev/null
  echo "Generated and stored a new DB password"
fi

aws ssm put-parameter --name "/${APP_NAME}/ANTHROPIC_API_KEY" --value "$ANTHROPIC_API_KEY" \
  --type SecureString --region "$AWS_REGION" --overwrite >/dev/null
echo "SSM parameter ready: /${APP_NAME}/ANTHROPIC_API_KEY"

# Tracing degrades to a no-op when these are placeholders (Stage 1), so the
# container starts cleanly either way.
put_optional () {
  local name="$1" value="${2:-}"
  if [ -n "$value" ]; then
    aws ssm put-parameter --name "/${APP_NAME}/${name}" --value "$value" \
      --type SecureString --region "$AWS_REGION" --overwrite >/dev/null
  else
    aws ssm get-parameter --name "/${APP_NAME}/${name}" --region "$AWS_REGION" >/dev/null 2>&1 || \
      aws ssm put-parameter --name "/${APP_NAME}/${name}" --value "unset" \
        --type SecureString --region "$AWS_REGION" >/dev/null
  fi
}
put_optional LANGFUSE_HOST       "${LANGFUSE_HOST:-}"
put_optional LANGFUSE_PUBLIC_KEY "${LANGFUSE_PUBLIC_KEY:-}"
put_optional LANGFUSE_SECRET_KEY "${LANGFUSE_SECRET_KEY:-}"
echo "SSM parameters ready for LANGFUSE_*"

# ------------------------------------------------------------ network lookup
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --region "$AWS_REGION" --query "Vpcs[0].VpcId" --output text)
SUBNET_IDS=$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC_ID" \
  --region "$AWS_REGION" --query "Subnets[].SubnetId" --output text | tr '\t' ',')
echo "VPC: $VPC_ID"
echo "Subnets: $SUBNET_IDS"

# ------------------------------------------------------------- image to run
# Reuse whatever is currently deployed so an infra-only update cannot roll the
# service back to the bootstrap tag (which does not exist in ECR until the
# first real push).
EXISTING_IMAGE=$(aws ecs describe-task-definition --task-definition "${APP_NAME}-task" \
  --region "$AWS_REGION" --query "taskDefinition.containerDefinitions[0].image" \
  --output text 2>/dev/null || echo "None")
if [ "$EXISTING_IMAGE" != "None" ] && [ -n "$EXISTING_IMAGE" ]; then
  CONTAINER_IMAGE="$EXISTING_IMAGE"
  echo "Reusing currently-running image: $CONTAINER_IMAGE"
else
  CONTAINER_IMAGE="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${APP_NAME}:bootstrap"
  echo "No task definition yet — seeding with placeholder: $CONTAINER_IMAGE"
fi

# --------------------------------------------------------------- the deploy
# Path handling on Windows, the short version: MSYS_NO_PATHCONV above is needed
# so Git Bash does not mangle the SSM parameter names, but it also stops it
# translating this file path for the AWS CLI (a Windows binary that cannot read
# "/e/..."). cygpath is not a reliable fix either — whichever one is first on
# PATH wins, and Anaconda's resolves "/e/" to "C:\Users\...\anaconda3\Library\e".
# A RELATIVE path sidesteps all of it: no leading slash means nothing to convert,
# and the CLI resolves it against the working directory.
cd "$SCRIPT_DIR"

# RDS creation takes 10-15 minutes on a first run. This is the long pole.
echo ""
echo "Deploying stack (first run creates RDS — expect 10-15 minutes)..."
aws cloudformation deploy \
  --template-file "cloudformation/agent-infra.yaml" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
    AppName="$APP_NAME" \
    GitHubOrg="$GITHUB_ORG" \
    GitHubRepo="$GITHUB_REPO" \
    GitHubOidcProviderArn="$OIDC_ARN" \
    VpcId="$VPC_ID" \
    SubnetIds="$SUBNET_IDS" \
    ContainerImage="$CONTAINER_IMAGE" \
    DbPassword="$DB_PASSWORD"

out () {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

echo ""
echo "==================================================================="
echo "Stack deployed: $STACK_NAME"
echo ""
echo "  ALB URL       : http://$(out AlbDnsName)"
echo "  ECR repo      : $(out EcrRepositoryUri)"
echo "  Cluster       : $(out EcsClusterName)"
echo "  Service       : $(out EcsServiceName)"
echo "  RDS endpoint  : $(out DbEndpoint)"
echo ""
echo "Add these as GitHub repo VARIABLES (Settings > Secrets and variables >"
echo "Actions > Variables). The role ARN is safe as a plain variable — OIDC"
echo "trust is scoped to this exact repo, and it is not a credential."
echo ""
echo "  AWS_DEPLOY_ROLE_ARN = $(out GitHubDeployRoleArn)"
echo "  AWS_REGION          = ${AWS_REGION}"
echo ""
echo "NEXT: push an image, then run  bash infra/run_ingest.sh"
echo "The service stays unhealthy until both are done — the database is empty"
echo "and the bootstrap image tag does not exist yet."
echo "==================================================================="
