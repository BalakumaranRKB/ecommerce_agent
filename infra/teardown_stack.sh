#!/usr/bin/env bash
#
# One command down (PLAN §2.6.5). Deletes the stack and the SSM parameters that
# live outside it. The GitHub OIDC provider is never touched — it is
# account-wide and other repos may depend on it (see setup_oidc_provider.sh).
#
# AFTER RUNNING THIS, OPEN THE CONSOLE AND VERIFY. The assignment requires a
# tested teardown, not an attempted one. Specifically check:
#   - RDS instance gone AND no snapshot left behind (the template sets
#     DeletionPolicy: Delete, but confirm — a stray snapshot bills quietly)
#   - ALB gone, ECR repository gone, /ecs/<app> log group gone
#   - no unassociated Elastic IP (billed hourly even when detached)

set -euo pipefail

# See deploy_stack.sh — Git Bash rewrites leading-slash arguments into Windows
# paths, which breaks the SSM parameter names below.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

AWS_REGION="${AWS_REGION:-ap-south-1}"
APP_NAME="${APP_NAME:-ordercare-agent}"
STACK_NAME="${APP_NAME}-infra"

echo "Deleting stack: $STACK_NAME  (region $AWS_REGION)"
aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$AWS_REGION"
echo "Waiting for delete to complete (RDS teardown takes several minutes)..."
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$AWS_REGION"
echo "Stack deleted."

for name in ANTHROPIC_API_KEY DB_PASSWORD LANGFUSE_HOST LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY; do
  aws ssm delete-parameter --name "/${APP_NAME}/${name}" --region "$AWS_REGION" >/dev/null 2>&1 \
    && echo "Deleted SSM parameter: ${name}" || echo "No SSM parameter ${name} to delete"
done

echo ""
echo "Checking for leftovers that bill silently..."
aws rds describe-db-snapshots --region "$AWS_REGION" \
  --query "DBSnapshots[?starts_with(DBInstanceIdentifier, '${APP_NAME}')].DBSnapshotIdentifier" \
  --output text || true
aws ec2 describe-addresses --region "$AWS_REGION" \
  --query "Addresses[?AssociationId==null].PublicIp" --output text || true

echo ""
echo "Teardown complete. Two lines above should both be EMPTY — anything listed"
echo "is a snapshot or an unassociated Elastic IP still costing money."
echo "(GitHub OIDC provider left in place, intentionally.)"
