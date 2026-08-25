# OrderCare — Ecommerce Support Agent (Assignment 3)

Migration of the Assignment 2 support agent onto managed AWS services: model inference on
**Amazon Bedrock**, retrieval on a **Bedrock Knowledge Base (Aurora PostgreSQL Serverless +
pgvector)**, and hosting/session-state on **Amazon Bedrock AgentCore**, plus a Path-A specialist
billing agent over A2A.

This README doubles as the running submission log. Findings are recorded per phase as the work
lands; detailed write-ups live under `docs/`.

---

## Phase 0 — Environment setup (status & findings)

Full ledger: [`docs/phase-0-findings.md`](docs/phase-0-findings.md). Summary below.

| Item | Status |
|------|--------|
| Branch `assignment-3` off A2 end-state + enriched `mock_data.py` | Done (ensure committed) |
| Bedrock model access (`claude-sonnet-5`, `claude-haiku-4.5`, Mumbai) | Done |
| Aurora Serverless v2 vector store (via Bedrock KB quick-create) | Done |
| AgentCore session-state status | Done — finding recorded |
| `agentcore` CLI + `bedrock-agentcore` + `a2a-sdk[http-server]` | **Pending** |

**Retrieval finding (headline).** The managed KB embeds with **Titan Text Embeddings v2
(1024-dim)** — A2's local `all-MiniLM-L6-v2` (384-dim) is not offered, so the model changed of
necessity. Two consequences, both expected §3.3 findings: (1) Titan *semantic-only* mis-ranks the
corpus (correct doc for a return question landed 5th at 0.74; **hybrid search** fixes it to #1),
and (2) the A2 floor `MIN_SIMILARITY = 0.32` **does not transfer** — gap questions score ~0.50 on
Titan, well above 0.32, so keeping it would break the honest-gap behaviour. The floor is
re-derived under hybrid in Phase 3. Full reasoning:
[`docs/similarity-threshold-migration.md`](docs/similarity-threshold-migration.md).

**AgentCore session-state finding (headline).** AgentCore Runtime **managed session storage** —
the stop/resume persistence §5's HITL flow depends on — is in **Public Preview** (Aug 2026), with
**1 GB/session and 14-day retention**. The platform, Memory, and CLI are GA; only the session
storage is preview, and it is preview platform-wide (not a Mumbai-specific gap). **Implication:** a
paused HITL flow can rely on parked state for at most 14 days with no preview SLA, so §5 designs
short resume windows and keeps a durable copy of critical state. Details and source in
[`docs/phase-0-findings.md`](docs/phase-0-findings.md).

---

## Phase 1 — Autonomy / A2A (complete)

Path A chosen — see [`docs/autonomy-decision.md`](docs/autonomy-decision.md)
and the dropped-handoff CWP in [`docs/dropped-handoff-found.md`](docs/dropped-handoff-found.md).

---

## Phase 2 — Compute migration: Bedrock + AgentCore (complete)

**Model swap.** Local development uses `AnthropicProvider` (direct Anthropic API) or `GroqProvider`
(free-tier Groq). The deployed path uses `BedrockProvider` — the same Claude Sonnet 5 model served
through Amazon Bedrock's `converse` API (`anthropic.claude-sonnet-5` in `ap-south-1`). The swap is
isolated to `llm.py`: `BedrockProvider` implements the same three-method `Provider` interface
(`create`, `append_assistant_turn`, `append_tool_results`) and returns the same `ModelResponse`/
`ToolCall` shapes. Nothing downstream — harness, agent, tools, RAG — was touched. That is the
provider seam working as designed.

**AgentCore hosting.** `agentcore_app.py` is the managed-runtime entry point. It wraps
`BedrockAgentCoreApp`, constructs a fresh `Session` per invocation (same stateless rule as
`server.py`), and calls `answer_turn()`. The harness loop, tools, and safety checks are reused
unchanged.

**AgentCore session-state finding.** AgentCore Runtime managed session storage is in **Public
Preview** (Aug 2026), with 1 GB/session and 14-day retention. The platform, Memory, and CLI are GA;
only session storage is preview. Implication for Phase 5 (HITL): a paused flow can rely on parked
state for at most 14 days with no preview SLA — design short resume windows and keep a durable copy.
Full details in [`docs/phase-0-findings.md`](docs/phase-0-findings.md).

**ECS not retired.** The old ECS/Fargate setup remains running until Phase 8, after the full new
path (compute + retrieval) is verified end to end.

| File | Change |
|------|--------|
| `llm.py` | Added `BedrockProvider(Provider)`, registered in `get_provider("bedrock")` |
| `agentcore_app.py` | New — AgentCore entry point wrapping `BedrockAgentCoreApp` |
| `tools_schema.py` | Added `_to_bedrock_tools()` and `"bedrock"` in `TOOLS_BY_PROVIDER` |
| `cli.py` | Added `"bedrock"` to `--provider` choices |

---

## Findings to come (later phases)

- **Phase 3 (retrieval):** final `MIN_SIMILARITY` under hybrid, the `strip_specifics()` decision,
  and the "migration is the live path" proof (old RDS path shown out of the request path).
- **Phase 8 (teardown):** ECS/Fargate retired after the new path is verified end to end.
