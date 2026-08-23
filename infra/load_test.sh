#!/usr/bin/env bash
#
# Stage 11b - trigger a real scale-out event against the deployed ALB.
#
# WHY IT RUNS THIS LONG
# Target-tracking scaling reacts to a CloudWatch metric that is published on a
# ~1 minute granularity, and the alarm needs several consecutive breaching
# periods before it fires. PLAN section 13: scale-out takes ~3 minutes,
# scale-in ~15. A 30-second burst shows nothing and leads you to conclude the
# policy is broken when it is simply not finished thinking yet. Default here is
# 6 minutes, which leaves margin over the ~3 minutes needed.
#
# WHY THIS MUCH CONCURRENCY
# The policy targets 20 requests per target per minute. Each ticket takes ~10s
# (mostly blocked on the LLM), so a single sequential client produces only ~6
# requests/min - below target, and nothing would ever scale. 12 concurrent
# workers produce roughly 70/min against one task, comfortably past the line.
#
# COST NOTE: every request is a real Anthropic call. Six minutes at this
# concurrency is a few hundred requests - cents, not dollars, but not free.
#
#   bash infra/load_test.sh                # 6 minutes, 12 workers
#   DURATION=300 WORKERS=8 bash infra/load_test.sh

set -uo pipefail   # deliberately NOT -e: a failed curl must not kill the run

ALB="${ALB:-http://ordercare-agent-alb-1863055997.ap-south-1.elb.amazonaws.com}"
DURATION="${DURATION:-360}"
WORKERS="${WORKERS:-12}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
CLUSTER="${CLUSTER:-ordercare-agent-cluster}"
SERVICE="${SERVICE:-ordercare-agent-service}"

# Varied messages across ticket types so the load looks like real traffic
# rather than one cached question repeated.
MESSAGES=(
  "Where has my order ord_5001 got to?"
  "My parcel ord_5001 is 10 days late, do I get anything for that?"
  "Can I still return this? It arrived three weeks ago."
  "How much do I lose if I refund opened electronics?"
  "Why is my account flagged and how do I appeal it?"
  "Can I change the delivery address after ordering?"
)

echo "======================================================================"
echo "  Target   : $ALB"
echo "  Duration : ${DURATION}s   Workers: $WORKERS"
echo "======================================================================"
echo ""
echo "Watch the task count rise in a SECOND terminal with:"
echo "  bash infra/watch_tasks.sh"
echo "and in the ECS console: Clusters > $CLUSTER > $SERVICE"
echo ""

END=$((SECONDS + DURATION))

worker () {
  local id="$1"
  while [ $SECONDS -lt $END ]; do
    local msg="${MESSAGES[$((RANDOM % ${#MESSAGES[@]}))]}"
    curl -s -o /dev/null -m 60 -X POST "$ALB/chat" \
      -H "Content-Type: application/json" \
      -d "{\"ticket_id\":\"tkt_load_${id}_$RANDOM\",\"customer_id\":\"cust_1001\",\"message\":\"$msg\"}"
  done
}

for i in $(seq 1 "$WORKERS"); do
  worker "$i" &
done

# Progress line plus the live task count, so the terminal itself shows the
# scale-out happening even without the console open.
while [ $SECONDS -lt $END ]; do
  sleep 20
  COUNT=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
            --region "$AWS_REGION" --query "services[0].runningCount" --output text 2>/dev/null || echo "?")
  DESIRED=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
            --region "$AWS_REGION" --query "services[0].desiredCount" --output text 2>/dev/null || echo "?")
  printf "  %3ds remaining   running=%s desired=%s\n" "$((END - SECONDS))" "$COUNT" "$DESIRED"
done

wait
echo ""
echo "Load stopped."
echo ""
echo "Task count stays elevated for ~15 minutes - scale-in cooldown is 300s and"
echo "the alarm needs sustained low traffic before it fires. That is expected,"
echo "not a stuck service. Capture the console NOW while the count is still up."
