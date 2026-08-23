"""
HTTP surface — POST /chat and GET /health. Nothing else.

PLAN-assignment-2.md §5.6 and §6.7. This is a new entry point around the same
agent, not a redesign of it: `cli.py` still works, `Session` is unchanged, and
the harness boundary is exactly where it was.

WHY /health TOUCHES NOTHING
The ALB health-checks this route every 15 seconds against every running task. If
it called the LLM, the database, or MCP, it would start timing out under exactly
the load that triggers scale-out — and the ALB would respond by killing healthy
tasks at the worst possible moment. It returns a static 200 and does no work.

WHY THE MODEL AND PROVIDER LOAD AT STARTUP, NOT PER REQUEST
PolicyRetriever loads all-MiniLM-L6-v2, which is expensive. Doing that per
request would add seconds to every call. They are built once in the lifespan
handler and shared; the request path only constructs a Session.

WHY A FRESH SESSION PER REQUEST
Conversation state must not leak between tickets — the same reasoning as the
eval's fresh-Session-per-fixture rule. It also keeps the service stateless,
which is what lets the ALB send any request to any task without sticky sessions.
Multi-turn context, if it is ever wanted, belongs in the database rather than in
process memory.

    uv run uvicorn server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from agent import Session
from llm import get_provider
from ticket import Ticket
from tracing import ENABLED as TRACING_ENABLED
from tracing import flush as flush_traces

# Populated once at startup by the lifespan handler below.
_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    provider_name = os.getenv("LLM_PROVIDER", "anthropic")
    print(f"[server] provider={provider_name}  tracing={'on' if TRACING_ENABLED else 'off'}")
    print("[server] loading embedding model and policy index...")

    # Imported here rather than at module scope so that a database that is not
    # yet reachable fails at startup with a clear message, instead of at import
    # time inside uvicorn's reloader.
    from retriever import PolicyRetriever

    _state["provider_name"] = provider_name
    _state["provider"] = get_provider(provider_name)
    _state["retriever"] = PolicyRetriever()
    print("[server] ready on :8080")

    yield

    # Spans batch in a background thread; a container that stops promptly can
    # die before they are sent. Same trap cli.py guards against.
    flush_traces()


app = FastAPI(title="E-commerce Order-Support Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    ticket_id: str
    customer_id: str
    message: str
    ticket_type: str = "refund"  # initial type; the agent reclassifies per turn


@app.get("/health")
def health() -> dict:
    """Static 200. Deliberately does no work — see the module docstring."""
    return {"status": "ok"}


@app.post("/chat")
def chat(req: ChatRequest) -> dict:
    """Run one ticket turn and return the answer WITH its trajectory.

    Returning the trajectory is not debug output: it is the same record the eval
    scores and the gate decides on (PLAN §6.7), so the deployed service, the
    eval, and a human curling the endpoint all see the identical evidence.
    """
    ticket = Ticket(
        ticket_id=req.ticket_id,
        customer_id=req.customer_id,   # the scoping authority — never taken from the message body
        ticket_type=req.ticket_type,
    )
    session = Session(
        ticket=ticket,
        provider=_state["provider"],
        provider_name=_state["provider_name"],
        retriever=_state["retriever"],
    )

    answer = session.answer_turn(req.message)
    traj = session.last_trajectory

    return {
        "answer": answer,
        "trajectory": traj.to_list() if traj else [],
        "trace_id": session.last_trace_id,
    }
