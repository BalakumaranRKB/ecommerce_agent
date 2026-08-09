# E-commerce Order-Support Agent

A harness-controlled, tool-connected, RAG-grounded customer-support agent for a
fixed e-commerce domain, built across two assignments for AI Systems in
Production. Assignment 1 built the agent; Assignment 2 added tracing, evaluation,
a CI regression gate, and a real AWS deployment.

Full staged build plan: [`docs/PLAN-assignment-2.md`](docs/PLAN-assignment-2.md).
Confident-wrong-path writeup: [`docs/confident-wrong-path.md`](docs/confident-wrong-path.md).

---

## 1. What this is

A multi-turn support agent that handles order-status, delivery, refund, and
subscription/account tickets for one customer at a time. For each message it:

1. **Classifies** the ticket type (small LLM call)
2. **Retrieves** the relevant policy from pgvector (cosine similarity, MiniLM-L6-v2)
3. **Looks up** the customer's order and account data through read-only tools
4. **Answers** — grounded in what it actually retrieved and looked up

The defining property: a **harness** (our code), not the model, decides whether
each proposed tool call is allowed to run. Every call is scoped to the ticket's
customer *before* dispatch — the model can only ever *propose*, and a
cross-customer request never reaches a tool.

### What Assignment 2 added

| Layer | What | Where |
|---|---|---|
| **Tracing** | LangFuse spans on every turn, nested (classify → retrieve → harness → dispatch) | `tracing.py` |
| **Trajectory eval** | 12 fixtures, rule-based, deterministic — scores the path, not the answer | `eval/regression_eval.py`, `eval/fixtures.json` |
| **LLM-as-judge** | Fact-checking rubric (0-10), 5 fixtures × 3 runs, reports but never gates | `eval/judge.py` |
| **CI gate** | GitHub Actions workflow, blocks deploy if pass rate drops >5pp from baseline | `.github/workflows/ci-cd.yml` |
| **AWS deployment** | ECS Fargate + ALB + RDS pgvector, autoscaling, OIDC — no static AWS keys | `infra/` |

---

## 2. Prerequisites

- [uv](https://docs.astral.sh/uv/) — project & dependency manager
  - **Windows (PowerShell):** `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
  - **macOS / Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Docker** (for local Postgres with pgvector)
- An **Anthropic API key** (`ANTHROPIC_API_KEY`)
- (For AWS deployment) AWS CLI configured + a GitHub repo with OIDC set up

---

## 3. Setup

```bash
# 1. Clone and checkout the assignment branch
git clone <your-repo-url>
cd ecommerce-agent-part2
git checkout assignment_2

# 2. Start local Postgres with pgvector
docker compose -f docker-compose.dev.yml up -d --wait

# 3. Install dependencies
uv sync

# 4. Configure environment
cp .env.example .env
# Edit .env — fill in ANTHROPIC_API_KEY (required)
# Set DB_PORT=55432 (local dev uses a non-default port to avoid conflicts)

# 5. Populate the database
uv run python ingest.py
# Creates schema, embeds 7 policy docs, seeds orders/accounts/prior tickets

# 6. Verify the provider
uv run python llm.py
```

---

## 4. How to run

### The agent (interactive)

```bash
uv run python cli.py                                       # cust_1001, refund
uv run python cli.py --customer cust_2002 --type account   # different customer
```

### The agent (HTTP server — same as the deployed version)

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8080

# Then:
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"ticket_id":"tkt_1","customer_id":"cust_1001","message":"Where is my order ord_5001?"}'
```

### Demo scenarios (the 7 scripted cases from Assignment 1)

```bash
uv run python scenarios.py               # run all seven
uv run python scenarios.py 1 6           # just scenarios 1 and 6
```

---

## 5. Tracing setup

**Tool:** LangFuse Cloud (US region, free tier).
**Integration:** `tracing.py` — the single module that knows about LangFuse.
Everything else imports `observe` / `trace_context` from here and never touches
the SDK directly.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | No | LangFuse project public key |
| `LANGFUSE_SECRET_KEY` | No | LangFuse project secret key |
| `LANGFUSE_HOST` | No | `https://us.cloud.langfuse.com` (US region) |

**When all three are unset or empty, tracing degrades silently to a no-op.**
The agent runs identically, the eval runs identically, CI runs identically —
no crash, no error. This is deliberate: the gate must not depend on a dashboard
being up.

### One-time setup

1. Create a project at [LangFuse Cloud](https://us.cloud.langfuse.com)
2. Copy the public/secret keys into your `.env`
3. Run `uv run python cli.py` and check the dashboard for a nested trace

### Span nesting

```
ticket:<type>              (root)
├─ classify                (LLM call)
├─ retrieve                (vector search)
└─ harness_loop            (tool-dispatch cycle)
   ├─ harness_check        (permission decision)
   └─ mcp_dispatch         (tool execution, if allowed)
```

---

## 6. Trajectory eval — how to run it and where things live

### Test fixtures

**File:** `eval/fixtures.json` — 12 fixtures across 4 ticket types, plus
honest-gap and cross-customer security cases.

Each fixture asserts on the **trajectory** (the steps the agent took), never on
the answer text. This is the design constraint from §4 of the plan — the
confident-wrong-path case (`delivery_damaged`) produced a well-written answer
while having retrieved entirely the wrong policy. An answer-text check would have
passed it.

### Stored baseline

**File:** `eval/baseline.json` — frozen at 12/12 = 100%, threshold 5.0pp,
captured on SHA `7c1828b7`.

### Running the eval

```bash
# Score the current agent against the baseline
uv run python -m eval.regression_eval --baseline

# Score only, no baseline comparison (development mode)
uv run python -m eval.regression_eval

# Single fixture
uv run python -m eval.regression_eval --only delivery_damaged

# Write a new baseline (run ONLY when you intend to re-freeze)
uv run python -m eval.regression_eval --write-baseline

# Save results to JSON
uv run python -m eval.regression_eval --json eval/results/run.json
```

### LLM-as-judge

```bash
# Run the fact-checking judge (5 fixtures × 3 runs, ~5 minutes)
uv run python -m eval.judge

# Save results
uv run python -m eval.judge --json eval/results/judge_scores.json
```

The judge scores how much of the agent's response is supported by the reference
policy docs. It runs separately from the gate and never blocks a deploy — it
reports to a human, not a machine. Scores and spread are in
`eval/results/judge_scores.json`.

### Reproducing the confident-wrong-path finding

```bash
# Deterministic, no LLM, no API key — re-runs the failing queries
uv run python -m eval.reproduce_cwp

# With the hypothesis (strip specifics before embedding)
uv run python -m eval.reproduce_cwp --hypothesis
```

---

## 7. CI gate — configuration and threshold

### Workflow file

**File:** `.github/workflows/ci-cd.yml`

**Trigger:** push to `assignment_2`, pull request targeting `assignment_1`.

### What it checks

The `eval-gate` job:
1. Starts a `pgvector/pgvector:pg16` service container
2. Installs dependencies with `uv sync`
3. Runs `uv run python ingest.py` (populates the DB)
4. Runs `uv run python -m eval.regression_eval --baseline`
5. If the pass rate drops more than **5.0 percentage points** from the committed
   baseline → exit code 1 → job fails → `build-and-push` and `deploy` jobs are
   blocked.

### The threshold: 5.0pp

With 12 fixtures, one ticket flipping is worth **8.3pp**. The threshold must sit
**below** one ticket's worth, or a single-fixture regression would be
undetectable by construction. At 5pp, any single fixture breaking (8.3pp drop)
exceeds the threshold and the gate fires.

The threshold is not higher because:
- The trajectory eval is deterministic (same query → same embedding → same
  result), verified across 5 consecutive clean runs before freezing.
- `claude-sonnet-5` does not accept a `temperature` parameter, so determinism is
  *verified empirically*, not enforced — but the 5-run check confirms no fixture
  wobbles at the pass/fail boundary.

The threshold is not 0pp because:
- Trajectories are not bit-identical across runs — classification can wobble
  (`order_status` vs `delivery`) — but this wobble never reaches the fixtures'
  pass/fail assertions. The 5pp margin absorbs incidental path variation.

Full writeup: `docs/choosing-the-baseline.md`.

### The pipeline

```
push → eval-gate → build-and-push → deploy
         │              │              │
         │         (OIDC to ECR)  (OIDC to ECS)
         │
    exit 1 = blocked
```

The `build-and-push` and `deploy` jobs both have `needs: eval-gate`. A regressed
trajectory never reaches ECR, let alone Fargate.

---

## 8. How to reproduce the before/after comparison

```bash
# Full run: executes the suite twice (clean + regressed), diffs the results
uv run python -m eval.report

# Re-render from saved results (no API calls)
uv run python -m eval.report --from-json
```

### The number

```
100.0% -> 66.7%, and 4 of 12 tickets flipped to FAIL:
  - order_status_basic          ! required tool 'lookup_order' was never dispatched
  - order_status_delivered      ! required tool 'lookup_order' was never dispatched
  - delivery_late_compensation  ! required tool 'lookup_order' was never dispatched
  - security_cross_customer     ! expected the harness to block a proposed call;
                                  no rejection recorded
```

The regression was `AGENT_REGRESSED=true` — an env var that withholds `lookup_order`
from the model's tool list. The gate caught it at 33.3pp (well past the 5pp threshold).

**Saved artifacts:**
- `eval/report_output.txt` — the rendered report
- `eval/report_before.json` / `eval/report_after.json` — raw run data

### The SHAs

| SHA | What | CI |
|---|---|---|
| `bb08ce5` | Deliberately regressed (lookup_order withheld) | 🔴 red, 8/12, −33.3pp |
| `6e52452` | Regression reverted | ✅ green, 12/12, 0.0pp |

---

## 9. Defensible justifications

### 1. Why the regression threshold is 5pp

The gate fails when the trajectory pass rate drops more than 5 percentage points
below the committed baseline. The suite has 12 fixtures, so a single ticket
flipping is worth 8.3pp — which means any threshold at or above 8.3 is
structurally incapable of detecting a single-ticket regression. One fixture
could break permanently without the gate ever firing.

The threshold therefore has to sit below the value of one ticket. 5pp leaves no
ambiguity: any single regression fails the build. Because the trajectory check
is rule-based over a recorded step list rather than a model output, it is
deterministic run to run (verified across 5 consecutive runs before freezing),
so there is no natural variance for a looser threshold to absorb — tightness
costs nothing here.

A looser setting (≥8.3pp) would let exactly the slow downward drift a relative
check exists to catch pass unnoticed. A tighter setting (0pp) would fail on
legitimate fixture re-scoping during development. If the suite grows past 12
fixtures, this arithmetic changes and the number gets revisited.

### 2. What the gate checks, and what it cannot catch

The gate scores the **trajectory** — the ordered list of retrieval steps, tool
calls, and harness rejections the agent actually performed — against per-fixture
`requires` and `forbids` rules. It never scores the final answer text.

This is deliberate: the confident-wrong-path case documented in
`docs/confident-wrong-path.md` produced an answer that reads as entirely correct.
A final-answer gate would have waved it straight through, rebuilding the same
blind spot one layer higher. That case is now a permanent fixture whose failure
conditions encode the specific retrieval flaw.

**What this choice cannot catch:** an answer that takes every required step and
then still phrases the result badly, mis-states a number it correctly retrieved,
or adopts the wrong tone. Those are real failure modes and the trajectory check
is blind to all of them — which is why the LLM-as-judge exists alongside it,
with a fact-checking rubric scoring whether every claim in the response appears
in the reference doc. The judge is reported rather than gated on, because judge
scores vary between runs on identical input (observed spread: 0-2 points across
3 runs) and a non-deterministic gate is a flaky gate, which is a gate people
learn to ignore.

---

## 10. AWS architecture

### Stack name

`ordercare-agent-infra` (CloudFormation, `infra/cloudformation/agent-infra.yaml`)

### Resources

| Resource | Name / Identifier | Detail |
|---|---|---|
| **ECS Cluster** | `ordercare-agent-cluster` | |
| **ECS Service** | `ordercare-agent-service` | Fargate, `awsvpc` networking |
| **Task Definition** | `ordercare-agent-task` | 1 vCPU / 2 GB memory |
| **ALB** | `ordercare-agent-alb` | Internet-facing, port 80 |
| **ALB DNS** | `ordercare-agent-alb-1863055997.ap-south-1.elb.amazonaws.com` | Stable URL, survives redeploys |
| **Target Group** | `ordercare-agent-tg` | `ip` target type, port 8080, health check `/health` |
| **ECR Repository** | `ordercare-agent` | Tagged by commit SHA (`$GITHUB_SHA`) |
| **RDS Instance** | `ordercare-agent-db` | PostgreSQL 16, `db.t4g.micro`, single-AZ |
| **Region** | `ap-south-1` | |

### How pgvector is wired into retrieval

- `CREATE EXTENSION IF NOT EXISTS vector` runs in `db.init_schema()`
- Policy doc embeddings (384-dim, MiniLM-L6-v2) stored in `policy_chunks` table
  with a `vector(384)` column
- Retrieval uses `1 - (embedding <=> query_embedding)` for cosine similarity
- No ANN index — at 7 rows, sequential scan beats any index structure
- `MIN_SIMILARITY = 0.32` threshold separates covered from uncovered questions

### Autoscaling

| Setting | Value | Justification |
|---|---|---|
| **Metric** | `ALBRequestCountPerTarget` | CPU is wrong for this workload — the agent spends ~95% of wall time blocked on LLM network calls, not computing. CPU never spikes. |
| **Target** | 20 requests/target/minute | Low because each ticket takes ~10s. A handful of concurrent clients crosses it. |
| **Min tasks** | 1 | Demo stack, torn down same day. Production would use 2 for AZ redundancy. |
| **Max tasks** | 4 | Cost ceiling. The demo only needs to show the count *rising*. |
| **Scale-out cooldown** | 60s | |
| **Scale-in cooldown** | 300s | |

### Security groups

```
Internet → ALB SG (port 80) → ECS SG (port 8080, ALB only) → DB SG (port 5432, ECS only)
```

Tasks are **not** reachable from `0.0.0.0/0`. The database is `PubliclyAccessible: false`.

### Secrets management

Secrets (`ANTHROPIC_API_KEY`, `DB_PASSWORD`, LangFuse keys) are stored in AWS
SSM Parameter Store as `SecureString` and injected into the container via the
task definition's `Secrets` block. They never appear in the CloudFormation
template, the workflow YAML, or any committed file.

---

## 11. Deploy pipeline

### Workflow file

`.github/workflows/ci-cd.yml` — three jobs:

1. **`eval-gate`** — trajectory regression gate (blocks on failure)
2. **`build-and-push`** — builds the Docker image, pushes to ECR (tagged by SHA)
3. **`deploy`** — registers new task definition, updates the ECS service, waits
   for stability

### OIDC — no static AWS keys

The `build-and-push` and `deploy` jobs authenticate via GitHub OIDC:

```yaml
- uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
    aws-region: ${{ vars.AWS_REGION }}
```

The role ARN is a repo **variable** (not a secret) — it is an identifier, not a
credential. The trust policy is scoped to this exact repo via the OIDC `sub`
claim.

**There are no `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` values anywhere in
this repository** — not in secrets, not in the workflow, not in any committed file.

---

## 12. Teardown

```bash
bash infra/teardown_stack.sh
```

This command:
1. Deletes the CloudFormation stack (`ordercare-agent-infra`)
2. Waits for deletion to complete (RDS teardown takes several minutes)
3. Deletes the SSM parameters (`ANTHROPIC_API_KEY`, `DB_PASSWORD`, `LANGFUSE_*`)
4. Checks for leftover resources that bill silently (snapshots, Elastic IPs)

**After running, open the AWS console and verify nothing remains:**
- RDS instance gone, no snapshot left behind
- ALB gone, ECR repo gone
- No unassociated Elastic IP
- `/ecs/ordercare-agent` log group gone

The GitHub OIDC provider is **not** deleted — it is account-wide and other repos
may depend on it.

---

## 13. Project structure

| Path | Purpose |
|---|---|
| `agent.py` | Orchestration: classify → memory → RAG → harness loop |
| `llm.py` | Provider abstraction (Anthropic / Groq) → normalized response |
| `harness.py` | The control loop + `harness_check` — **the boundary** |
| `retriever.py` | pgvector RAG; `strip_specifics` + honest-gap threshold |
| `db.py` | All SQL — connection management, schema DDL, every query |
| `tracing.py` | LangFuse integration — the single module that knows about tracing |
| `server.py` | FastAPI HTTP surface (POST `/chat`, GET `/health`) |
| `cli.py` | Interactive conversation entry point |
| `ingest.py` | One-shot DB population: embed docs, seed reference data |
| `memory.py` | Short-term conversation buffer + long-term history lookup |
| `mcp_server.py` / `mcp_client.py` | MCP tools (lookup_order, check_account_status) |
| `tools_schema.py` | Tool specs advertised to the model |
| `ticket.py` | Active ticket / session context |
| `policy_docs/` | 7 Markdown policy docs |
| `eval/fixtures.json` | 12 test fixtures (data, not code) |
| `eval/baseline.json` | Frozen baseline for the CI gate |
| `eval/regression_eval.py` | Trajectory eval + gate logic |
| `eval/judge.py` | LLM-as-judge (fact-checking rubric) |
| `eval/report.py` | Before/after comparison report |
| `eval/reproduce_cwp.py` | Confident-wrong-path reproducer (no LLM) |
| `docs/confident-wrong-path.md` | The CWP writeup |
| `docs/choosing-the-baseline.md` | Why 5pp, why relative |
| `infra/cloudformation/agent-infra.yaml` | The entire AWS stack as IaC |
| `infra/deploy_stack.sh` | One command up |
| `infra/teardown_stack.sh` | One command down |
| `infra/run_ingest.sh` | Populate RDS via a one-off Fargate task |
| `infra/load_test.sh` | Trigger scale-out for the demo |
| `.github/workflows/ci-cd.yml` | CI: eval-gate → build → deploy |
| `Dockerfile` | Agent image for Fargate (CPU-only torch, baked model) |
