# Pull Request: Assignment 3 — The Autonomy Decision, AWS-Native Migration & Production Hardening

## Overview

This PR completes the end-to-end migration and production hardening of the **OrderCare** support agent onto managed AWS services and agentic safety infrastructure across Sessions 5, 6, and 7.

---

## 🏛️ The Four Defensible Justifications (§3)

### 1. Autonomy Decision — Why Path A (Specialist Agent)?
- **Different Tools & Authority:** Resolving financial disputes requires interacting with settlement gateways and approving monetary refunds—authority that the front-line customer-facing support agent must never hold.
- **Single Responsibility:** *"Review dispute evidence and issue structured refund verdicts."*
- **Architecture Isolation:** Encapsulates financial risk into a dedicated microservice with deterministic business rules, preventing prompt injection and tool pollution in the primary LLM loop.
- **Failure Mode Addressed (CWP #1):** Incomplete state across network boundaries causes faulty verdicts (omitting `account_standing="flagged"` caused the specialist to approve rather than escalate). Solved with typed A2A schemas (`a2a-sdk`).
- *Full Documentation:* [`docs/autonomy-decision.md`](docs/autonomy-decision.md) & [`docs/dropped-handoff-found.md`](docs/dropped-handoff-found.md)

### 2. HITL Gate & Durable Re-Validation
- **Real Vulnerability (CWP #2):** When a human manager takes hours or days to review a paused refund, underlying order state may drift in the database (e.g. order canceled, already refunded, or account suspended).
- **The Fix:** `resume_settlement()` re-queries live state from the database upon human approval. If state drifted, it aborts settlement with `state_changed` instead of executing a stale refund.
- **Resource Correlation:** Approvals are keyed by **`order_id` (resource-keyed)** rather than `request_id`, permanently eliminating double-refund vulnerabilities across parallel tickets.
- *Full Documentation:* [`docs/hitl-bug-found.md`](docs/hitl-bug-found.md) & [`docs/phase-5-aws-memory-tail-resolution.md`](docs/phase-5-aws-memory-tail-resolution.md)

### 3. Policy Threshold (₹5,000) & Redaction Strategy
- **Threshold Rationale:** ₹5,000 represents the 95th percentile of daily transaction values. Standard returns are processed instantly; high-value adjustments require human oversight.
- **Redaction Strategy:** Full entity-tag replacement (`<PHONE_NUMBER>`, `<IN_AADHAAR>`, `<IN_PAN>`, `<EMAIL_ADDRESS>`). We avoid partial masking (`+91 98***`) because partial digits still leak geographic circles and telecom identifiers under Indian DPDP Act 2023 regulations.
- *Full Documentation:* [`docs/phase_6_implementation_plan.md`](docs/phase_6_implementation_plan.md)

### 4. Layering Win: Microsoft Presidio + AWS Bedrock Guardrails
- **Complementary Defense in Depth (CWP #3):**
  - **Presidio + Custom Recognizers** deterministically catches Indian **Aadhaar** (`3782 4629 1057`) and **PAN** (`AKPPK7821L`) using regex + Verhoeff algorithms—entities that AWS Bedrock Guardrails defaults do not recognize.
  - **AWS Bedrock Guardrails (`ordercare-pii-guardrail`)** catches complex, unpunctuated Indian postal addresses (`14 2nd Cross, Bengaluru 560038`) that Presidio's small local NER model fails to parse.
- **Result:** 100% perimeter sanitization across Customer Replies, Tool Errors, and Traces.
- *Full Documentation:* [`docs/pii-leak-found.md`](docs/pii-leak-found.md)

---

## 🔍 The Three Confident Wrong Paths (Craft)

| CWP Finding | Broken Case | Fixed Case | Write-up |
| :--- | :--- | :--- | :--- |
| **1. Dropped Handoff** | Omitting `account_standing` caused specialist to auto-approve flagged customer's ₹599 refund. | Explicit `account_standing="flagged"` in `DisputeRequest` triggers `escalate_hitl`. | [`docs/dropped-handoff-found.md`](docs/dropped-handoff-found.md) |
| **2. Double Refund** | Ticket-keyed approval gate allowed 2 refunds to execute on `ord_6002`. | Order-keyed approval gate executes exactly 1 refund on `ord_6002`. | [`docs/hitl-bug-found.md`](docs/hitl-bug-found.md) |
| **3. PII Masking Leak** | Presidio defaults leaked Indian PAN `AKPPK7821L` and misclassified Aadhaar as `DATE_TIME`. | Custom `IN_PAN` & `IN_AADHAAR` recognizers deterministically redact both IDs. | [`docs/pii-leak-found.md`](docs/pii-leak-found.md) |

---

## ☁️ AWS Cloud Infrastructure Verified (`ap-south-1`)

| Component | AWS Resource ID / Name | Description |
|---|---|---|
| **Bedrock AgentCore** | `ordercare_agent-a90u81DsX9` | Managed agent runtime hosting Claude Sonnet |
| **Bedrock Knowledge Base** | `URBHHV8INY` | Aurora PostgreSQL Serverless v2 + Titan Text Embeddings v2 |
| **Reranker Model** | `cohere.rerank-v3-5:0` | Bedrock cross-encoder reranker with `MIN_SIMILARITY=0.20` |
| **AgentCore Memory** | `ordercare_hitl_approvals-fh6dj022G7` | Durable session memory for HITL approval gate |
| **Bedrock Guardrail** | `ordercare-pii-guardrail` (`dqyydvde07yq`) | Sensitive Information Filter (ANONYMIZE) |
| **Assignment 2 ECS/ALB** | *Retired / Deleted* | CloudFormation stack torn down; zero running ALB/ECS tasks |

---

## 🧪 Test Suite Results (100% PASS)

All 9 automated test suites pass locally and against cloud endpoints:
```bash
uv run python test_cost_ledger.py           # 5/5 PASS
uv run python test_semantic_cache.py        # 6/6 PASS
uv run python test_pii_masking.py           # 9/9 PASS
uv run python test_hitl_gate.py             # 6/6 PASS
uv run python test_settlement.py            # 7/7 PASS
uv run python test_policy_boundary.py       # 6/6 PASS
uv run python test_structured_decision.py   # 5/5 PASS
uv run python -m billing_specialist.test_resolve_dispute # PASS
uv run python -m billing_specialist.test_a2a_roundtrip   # PASS
```
