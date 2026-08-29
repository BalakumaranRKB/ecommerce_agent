# OrderCare — Ecommerce Support Agent (Assignment 3)

Complete migration and hardening of the OrderCare support agent onto managed AWS services and agentic safety infrastructure:
- **Model Inference:** Amazon Bedrock (`anthropic.claude-sonnet-4-6` / `claude-sonnet-5` via Converse API)
- **Managed Retrieval:** Amazon Bedrock Knowledge Base (`URBHHV8INY` backed by Aurora PostgreSQL Serverless v2 + pgvector, 1024-dim Titan Text Embeddings v2, and Cohere Cross-Encoder Reranker v3.5)
- **Hosting & Durable State:** Amazon Bedrock AgentCore Runtime & AgentCore Memory Data Plane
- **Multi-Agent Specialist:** Path-A Billing Specialist over typed `a2a-sdk`
- **Structured Authority:** Tool-use enforced Pydantic decision models (`RefundDecision`, `PendingApproval`)
- **Policy Engine & HITL Gate:** Hard mechanical code boundary (₹5,000 threshold) with durable pause-and-resume in AWS Bedrock AgentCore Memory
- **Layered PII Defense:** Microsoft Presidio (in-process NER + Custom Indian Aadhaar/PAN recognizers) stacked with AWS Bedrock Guardrails (`ordercare-pii-guardrail`)
- **Cost Ledger & Semantic Cache:** In-process SQLite token/dollar ledger + cosine-similarity semantic cache delivering >85% latency reduction

---

## At a Glance: Build Status Across Phases

| Phase | Description | Status | Key Deliverable |
|---|---|---|---|
| **0** | Environment & Lead-Time Resources | ✅ Complete | Bedrock Access, Aurora KB, Mumbai Region |
| **1** | Multi-Agent Handoff (A2A) | ✅ Complete | Billing Specialist over A2A + Dropped-Handoff CWP |
| **2** | Compute Migration | ✅ Complete | `BedrockProvider` + `agentcore_app.py` |
| **3** | Retrieval Migration | ✅ Complete | Bedrock KB + Cohere Cross-Encoder Reranker (`MIN_SIMILARITY=0.20`) |
| **4** | Structured Decision Payloads | ✅ Complete | `RefundDecision` via tool-use (never regexed prose) |
| **5** | Policy Boundary & HITL Gate | ✅ Complete | ₹5,000 threshold + AgentCore Memory pause/resume + Double-Refund CWP |
| **6** | Layered PII Masking | ✅ Complete | Presidio + Bedrock Guardrail + Aadhaar/PAN CWP |
| **7** | Cost Ledger & Semantic Cache | ✅ Complete | `cost_ledger.py` (SQLite) + `semantic_cache.py` (>85% faster) |
| **8** | Retire ECS & Whole-Path Verification | ✅ Complete | ECS/ALB Stack retired, end-to-end live proven |

---

## The Four Defensible Justifications

### 1. Autonomy Decision — Why Path A (Specialist Agent)?
*Full write-up:* [`docs/autonomy-decision.md`](docs/autonomy-decision.md)
- **Different Tools/Authority:** Resolving billing disputes requires interacting with settlement gateways and approving financial adjustments—authority the front-line support agent must never hold.
- **One-Sentence Job:** *"Review dispute evidence and issue structured refund verdicts."*
- **Real Bottleneck:** Isolates high-risk financial authority into a dedicated process with strict input validation, preventing tool pollution in the customer-facing agent.
- **The Failure Mode (CWP #1):** Dropped state across the boundary leads to wrong verdicts (omitting `account_standing="flagged"` causes the specialist to approve rather than escalate). Solved with typed A2A schemas.

### 2. HITL Gate & Durable Re-Validation
*Full write-up:* [`docs/hitl-bug-found.md`](docs/hitl-bug-found.md)
- **What Re-Validation Protects Against (CWP #2):** When a human manager takes hours/days to approve a paused refund, underlying order state may drift (e.g. customer cancels, order is already refunded, or account gets suspended).
- **The Fix:** `resume_settlement()` re-queries live state from the database. If state drifted, it aborts settlement with `state_changed` instead of executing a stale refund.
- **Correlation Key:** Approvals are keyed by **`order_id` (resource-keyed)** rather than `request_id`, blocking double-refund attacks.

### 3. Policy Threshold (₹5,000) & Redaction Strategy
- **Threshold Rationale:** ₹5,000 represents the 95th percentile of daily ecommerce transactions. Standard returns are processed instantly by code; high-value adjustments require human manager oversight.
- **Redaction Strategy:** Full entity-tag replacement (`<PHONE_NUMBER>`, `<IN_AADHAAR>`, `<IN_PAN>`, `<EMAIL_ADDRESS>`). We do not use partial masking (`+91 98***`) because partial digits still leak geographic circles and telecom identifiers under Indian DPDP Act 2023 regulations.

### 4. Layering Win: Microsoft Presidio + AWS Bedrock Guardrails
*Full write-up:* [`docs/pii-leak-found.md`](docs/pii-leak-found.md)
- **The Complementary Win (CWP #3):** 
  - **Presidio with Custom Recognizers** deterministically catches Indian **Aadhaar** (`3782 4629 1057`) and **PAN** (`AKPPK7821L`) using regex + Verhoeff algorithms—entities that AWS Bedrock Guardrails defaults do not recognize.
  - **AWS Bedrock Guardrails (`ordercare-pii-guardrail`)** catches complex, unpunctuated Indian postal addresses (`14 2nd Cross, Bengaluru 560038`) that Presidio's small local NER model fails to parse.
- **Together:** 100% perimeter sanitization across Customer Replies, Tool Errors, and Tracing/Ledgers.

---

## Architecture Overview

```
                      ┌──────────────────────────────────────────────────┐
                      │              Customer / Ticket (CLI/UI)          │
                      └─────────────────────────┬────────────────────────┘
                                                │ Raw Message
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ BEDROCK AGENTCORE RUNTIME                                                                       │
│                                                                                                 │
│   ┌────────────────────────────────┐            ┌───────────────────────────────────────────┐   │
│   │ 1. Classification & Ledger     │            │ 2. Semantic Cache (semantic_cache.py)     │   │
│   │    (cost_ledger.py SQLite)     │            │    - Cosine Sim >= 0.65 (Titan v2)        │   │
│   └───────────────┬────────────────┘            └─────────────────────┬─────────────────────┘   │
│                   │                                                   │ (Miss -> Fetch)         │
│                   ▼                                                   ▼                         │
│   ┌────────────────────────────────┐            ┌───────────────────────────────────────────┐   │
│   │ 3. Main Agent (agent.py)       │ ◄───────── │ Amazon Bedrock Knowledge Base (URBHHV8INY)│   │
│   │    - System Prompt + Context   │            │   - Aurora PostgreSQL Serverless v2       │   │
│   └───────────────┬────────────────┘            │   - Cohere Cross-Encoder Reranker v3.5    │   │
│                   │ (A2A Handoff)               └───────────────────────────────────────────┘   │
│                   ▼                                                                             │
│   ┌────────────────────────────────┐                                                            │
│   │ 4. Billing Specialist (a2a)    │ ─────────► [5. Policy Boundary: policy.py (INR 5,000)]     │
│   │    - if/else business logic    │                                 │                          │
│   └────────────────────────────────┘                ┌────────────────┴────────────────┐         │
│                                                     ▼ (Allow)                         ▼ (Pause) │
│                                             ┌───────────────┐     ┌───────────────────────────┐ │
│                                             │ Settle Refund │     │ HITL Gate (hitl.py)       │ │
│                                             └───────────────┘     │ Bedrock AgentCore Memory  │ │
│                                                                   └───────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                │ Data Ready to Exit
                                                ▼
═══════════════════════════════════════════════════════════════════════════════════════════════════
  EGRESS SECURITY PERIMETER — mask(text) in pii.py (Presidio Layer 1 + Bedrock Guardrails Layer 2)
═══════════════════════════════════════════════════════════════════════════════════════════════════
        │                                       │                                   │
        ▼                                       ▼                                   ▼
  🚪 Customer Reply                      🚪 Tool/Specialist Errors           🚪 Observability/Traces
  (100% Sanitized)                       (mask(str(e)) Redacted)             (No PII in traces/DB)
```

---

## Quickstart & Verification Commands

### 1. Run All Automated Test Suites (Phases 1–7)
```bash
# Run the complete test suite
uv run python test_cost_ledger.py
uv run python test_semantic_cache.py
uv run python test_pii_masking.py
uv run python test_hitl_gate.py
uv run python test_settlement.py
uv run python test_policy_boundary.py
uv run python test_structured_decision.py
uv run python -m billing_specialist.test_resolve_dispute
uv run python -m billing_specialist.test_a2a_roundtrip
```

### 2. Run Cost Ledger Report
```bash
uv run python cost_ledger.py
```

### 3. Run Semantic Cache Demonstration
```bash
uv run python semantic_cache.py
```

### 4. Interactive CLI Conversation
```bash
uv run python cli.py --customer cust_1001 --type refund
```

---

## Cloud Resource Identifiers (AWS `ap-south-1`)

| Component | AWS Resource ID / Name | Notes |
|---|---|---|
| **Bedrock Knowledge Base** | `URBHHV8INY` | Aurora PostgreSQL Serverless v2 + Titan Text Embeddings v2 |
| **Cross-Encoder Reranker** | `cohere.rerank-v3-5:0` | Invoked in `us-east-1` for calibrated relevance scoring |
| **Bedrock Guardrail** | `ordercare-pii-guardrail` (`dqyydvde07yq`) | Sensitive Information Filters (ANONYMIZE) |
| **AgentCore Memory** | `ordercare_hitl_approvals-fh6dj022G7` | Bedrock AgentCore Data Plane durable HITL store |
| **Old ECS Stack** | *Retired / Deleted* | Confirmed gone; no running ALB or ECS clusters |
