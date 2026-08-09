#!/usr/bin/env bash
#
# Populate RDS: schema, the 7 embedded policy docs, and the mock order/account/
# history tables. Run ONCE after the stack exists and an image has been pushed.
#
# WHY THIS IS AN ECS TASK AND NOT A LAPTOP COMMAND
# The database is PubliclyAccessible: false and its security group only admits
# the ECS task security group, so `python ingest.py` from a laptop cannot reach
# it — by design (PLAN §13: unreachable from the internet regardless of subnet).
# Running ingest as a one-off Fargate task using the SAME image puts it inside
# the VPC with the right credentials and no bastion host, no NAT gateway, and no
# temporary hole in the security group.
#
# This is also why ingest is a command and not a startup hook (PLAN §5.5): the
# database is shared state, and every task re-embedding the same 7 rows on boot
# would be wasted work, a slow cold start, and a write race.

set -euo pipefail

# See deploy_stack.sh — Git Bash rewrites leading-slash arguments into Windows
# paths. Matters here for the ARNs and the JSON overrides passed to run-task.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

AWS_REGION="${AWS_REGION:-ap-south-1}"
APP_NAME="${APP_NAME:-ordercare-agent}"
STACK_NAME="${APP_NAME}-infra"

out () {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

CLUSTER=$(out EcsClusterName)
SG=$(out EcsSecurityGroupId)
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --region "$AWS_REGION" --query "Vpcs[0].VpcId" --output text)
SUBNET=$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC_ID" \
  --region "$AWS_REGION" --query "Subnets[0].SubnetId" --output text)

echo "Cluster: $CLUSTER   Subnet: $SUBNET   SG: $SG"
echo "Launching one-off ingest task..."

TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "${APP_NAME}-task" \
  --launch-type FARGATE \
  --region "$AWS_REGION" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"${APP_NAME}\",\"command\":[\"python\",\"ingest.py\"]}]}" \
  --query "tasks[0].taskArn" --output text)

echo "Task: $TASK_ARN"
echo "Waiting for it to finish (pulls the image first — allow a few minutes)..."
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN" --region "$AWS_REGION"

EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" --region "$AWS_REGION" \
  --query "tasks[0].containers[0].exitCode" --output text)
REASON=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" --region "$AWS_REGION" \
  --query "tasks[0].stoppedReason" --output text)

TASK_ID="${TASK_ARN##*/}"
echo ""
echo "Exit code: $EXIT_CODE   ($REASON)"
echo "Logs:"
echo "  aws logs tail /ecs/${APP_NAME} --log-stream-names ecs/${APP_NAME}/${TASK_ID} --region ${AWS_REGION}"

# ingest.py ends with verify() and returns non-zero if the database does not
# hold what the agent expects — so a bad ingest fails HERE, loudly, instead of
# surfacing later as an agent that answers every question with an honest gap.
if [ "$EXIT_CODE" != "0" ]; then
  echo ""
  echo "INGEST FAILED. Check the logs above before deploying."
  exit 1
fi
echo ""
echo "Ingest complete — RDS now holds the policy chunks and mock tables."
