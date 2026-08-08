# Agent image for ECS Fargate.
#
# PLAN-assignment-2.md Stage 9 + §13. Three things in here are load-bearing and
# each one is a trap from §13 that costs an hour if skipped:
#
#   1. --platform=linux/amd64      Fargate will not run arm64. On Apple Silicon
#                                  the failure is a task that starts and dies
#                                  instantly with "exec format error", visible
#                                  only in CloudWatch.
#   2. CPU-only torch              The default wheel bundles CUDA libraries that
#                                  are unusable on Fargate and multiply image
#                                  size by several GB.
#   3. Model baked at build time   Otherwise every cold start downloads ~90MB
#                                  from HuggingFace — at exactly the moment a
#                                  task is being spawned because load spiked and
#                                  it should be answering, not downloading.
#
# Build:  docker build --platform linux/amd64 -t ecommerce-agent .

FROM --platform=linux/amd64 python:3.11-slim

WORKDIR /app

# Trap 2. Installed FIRST and in its own layer: it is the largest download, and
# once torch is satisfied here, the sentence-transformers install below will not
# pull the default CUDA build.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Trap 3. Its own layer, before the source copy, so editing application code
# does not re-download the model on every rebuild.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY . .

# Must match ContainerPort in the task definition AND the target group's Port,
# or the ALB never registers a target and the service sits unhealthy forever.
EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
