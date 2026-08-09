#!/usr/bin/env bash
#
# Watch the ECS task count while load_test.sh runs. Meant for a second terminal.
# The ECS console is the artifact the assignment wants captured (PLAN section
# 2.6.3), but a console screenshot alone does not show the RISE - this gives a
# timestamped text record of the count changing, which is useful evidence
# alongside it and works if the console is slow to refresh.

set -uo pipefail

AWS_REGION="${AWS_REGION:-ap-south-1}"
CLUSTER="${CLUSTER:-ordercare-agent-cluster}"
SERVICE="${SERVICE:-ordercare-agent-service}"
INTERVAL="${INTERVAL:-15}"

echo "time      running  desired   (Ctrl-C to stop)"
echo "-------------------------------------------"
while true; do
  read -r RUNNING DESIRED <<< "$(aws ecs describe-services \
    --cluster "$CLUSTER" --services "$SERVICE" --region "$AWS_REGION" \
    --query "services[0].[runningCount,desiredCount]" --output text 2>/dev/null || echo "? ?")"
  printf "%s      %-8s %-8s\n" "$(date +%H:%M:%S)" "$RUNNING" "$DESIRED"
  sleep "$INTERVAL"
done
