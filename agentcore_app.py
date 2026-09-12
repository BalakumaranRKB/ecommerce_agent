"""
AgentCore entry point — the managed-runtime surface for the support agent.

Phase 2 (spec §2.2): wraps the same Session / answer_turn() logic that
server.py (ECS/Fargate) and cli.py (local) already use, but runs on Amazon
Bedrock AgentCore instead of hand-built infra.

Key design rule: this file constructs a Session and calls answer_turn() —
that's it. It does NOT touch harness.py, agent.py reasoning, or
harness_check(). If the harness loop needs rewriting to fit AgentCore,
that's the abstraction fighting back — stop and report it (spec §2.1).

Local test:  agentcore dev
Deploy:     agentcore launch
"""

from __future__ import annotations

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agent import Session
from llm import get_provider
from retriever import PolicyRetriever
from ticket import Ticket
from tracing import flush as flush_traces

# ── Startup: build the shared objects once ──────────────────────────────
print("[agentcore] loading provider and policy index...")

_provider_name = os.getenv("LLM_PROVIDER", "bedrock")  # default to bedrock on AgentCore
_provider = get_provider(_provider_name)
_retriever = PolicyRetriever()

print(f"[agentcore] provider={_provider_name}  ready")

# ── AgentCore app ───────────────────────────────────────────────────────
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """Handle one agent invocation.

    The payload is whatever the caller sends when invoking the AgentCore
    endpoint.  We expect at minimum:
      - message  (str)  — the customer's message
      - customer_id (str) — scoping authority
      - ticket_id  (str, optional) — defaults to a generated id
      - ticket_type (str, optional) — defaults to 'refund'
    """
    user_message = payload.get("message") or payload.get("prompt", "")
    customer_id = payload.get("customer_id", "cust_1001")
    ticket_id = payload.get("ticket_id", f"tkt_ac_{customer_id}")
    ticket_type = payload.get("ticket_type", "refund")

    ticket = Ticket(
        ticket_id=ticket_id,
        customer_id=customer_id,
        ticket_type=ticket_type,
    )

    # Fresh session per invocation — same stateless rule as server.py.
    session = Session(
        ticket=ticket,
        provider=_provider,
        provider_name=_provider_name,
        retriever=_retriever,
    )

    answer = session.answer_turn(user_message)
    traj = session.last_trajectory

    return {
        "answer": answer,
        "trajectory": traj.to_list() if traj else [],
        "trace_id": session.last_trace_id,
    }


if __name__ == "__main__":
    try:
        app.run()
    finally:
        flush_traces()
