"""
Tracing seam — one place that knows about LangFuse, so nothing else does.

Every module downstream imports `observe` and `trace_context` from here and
never touches the LangFuse SDK directly. That indirection buys three things:

1. TRACING IS OPTIONAL. If LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are absent
   — which is exactly the situation in CI — `observe()` degrades to a
   pass-through decorator and `trace_context()` to a no-op. The agent runs
   identically, just unobserved. This matters because the regression gate
   (Stage 6) runs the agent on a GitHub runner with no LangFuse instance: a
   tracing import that raised there would take the gate down with it.

2. VERSION CHURN IS CONTAINED. The SDK was rewritten in v4 (OTEL-native,
   March 2026) and the trace-attribute API moved with it. When it moves again,
   this file changes and nothing else does.

3. THE EVAL NEVER READS FROM HERE. LangFuse is for a human to click into. The
   trajectory the eval scores is recorded separately, in-process, and returned
   by the agent (see docs/PLAN-assignment-2.md §5.4). Same events, two
   audiences, no coupling — a gate that fails when a dashboard is down is
   worse than no gate.

v4 API notes (differs from most tutorials still showing v2/v3):
  - `propagate_attributes()` replaces `update_current_trace()`; `name` is now
    `trace_name`.
  - Propagated `metadata` must be dict[str, str], values <= 200 chars. Longer
    values are dropped with a warning, so put bulk data on the span (where
    @observe captures inputs/outputs automatically), not in metadata.
  - v4 filters non-LLM OpenTelemetry spans by default, so psycopg/FastAPI
    instrumentation will NOT clutter the trace tree. No denylist needed.

Smoke test (needs real keys in .env; prints the trace URL):
    uv run python tracing.py
"""

from __future__ import annotations

import os
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

# Presence of BOTH keys is the switch. Checked before importing the SDK so a
# missing/broken install can never break an unobserved run.
_HAS_KEYS = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))

ENABLED = False
_client = None
_lf_observe = None
_lf_propagate = None

if _HAS_KEYS:
    try:
        from langfuse import get_client
        from langfuse import observe as _lf_observe
        from langfuse import propagate_attributes as _lf_propagate

        _client = get_client()
        ENABLED = True
    except Exception as exc:  # noqa: BLE001 — never let tracing break the agent
        print(f"[tracing] disabled ({type(exc).__name__}: {exc})")
        ENABLED = False


def observe(name: str | None = None, **kwargs):
    """Span decorator. ALWAYS call with parentheses: @observe() or @observe(name=...).

    Enabled  -> delegates to langfuse's @observe, which captures the function's
                arguments as span input and its return value as span output.
                That auto-capture is why retrieval hits and tool arguments end
                up in the trace without any explicit attribute plumbing.
    Disabled -> returns the function untouched, zero overhead.
    """
    if not ENABLED:
        def _passthrough(func):
            return func
        return _passthrough

    if name is not None:
        kwargs["name"] = name
    return _lf_observe(**kwargs)


@contextmanager
def trace_context(ticket, name: str | None = None):
    """Open the ROOT span for one turn and attach ticket identity to everything
    beneath it.

    Order matters here, and getting it wrong is silent: `propagate_attributes()`
    needs an active span to attach to. Calling it before any span exists logs
    "No active span in current context" and skips the attributes. So this
    context manager starts the root observation FIRST, then propagates inside
    it — which is why the whole turn is wrapped in one `with` rather than
    relying on a decorator to have opened something already.

    Maps our domain onto LangFuse's built-in views so the UI's Sessions and
    Users tabs work without extra configuration:
        customer_id -> user_id      (one customer, many tickets)
        ticket_id   -> session_id   (one ticket, many turns)
        ticket_type -> tag          (filter the trace list by type)

    Yields the root span, so a caller can set its output if it wants to.
    """
    if not ENABLED:
        yield None
        return

    span_name = name or f"ticket:{getattr(ticket, 'ticket_type', None) or 'unclassified'}"

    attrs = {
        "trace_name": span_name,
        "user_id": ticket.customer_id,
        "session_id": ticket.ticket_id,
    }
    if getattr(ticket, "ticket_type", None):
        attrs["tags"] = [ticket.ticket_type]

    with _client.start_as_current_observation(as_type="span", name=span_name) as span:
        with _lf_propagate(**attrs):
            yield span


def current_trace_id() -> str | None:
    """The active trace id, or None when tracing is off.

    Returned by POST /chat (§6.7) so a response can be tied back to the trace
    that produced it — the link between "this answer was wrong" and the span
    tree that shows why.
    """
    if not ENABLED:
        return None
    try:
        return _client.get_current_trace_id()
    except Exception:  # noqa: BLE001 — a missing id is not worth an exception
        return None


def flush() -> None:
    """Force-send buffered spans. Call before a short-lived process exits.

    The exporter batches in a background thread, so a CLI run that finishes
    quickly can exit before anything is sent — the classic "my code ran but
    the dashboard is empty" symptom. Long-running servers don't need this.
    """
    if ENABLED and _client is not None:
        _client.flush()


if __name__ == "__main__":
    print(f"[tracing] ENABLED={ENABLED}")
    if not ENABLED:
        print("[tracing] no keys found — this is the CI path; the agent runs unobserved.")
        raise SystemExit(0)

    print(f"[tracing] host={os.getenv('LANGFUSE_HOST')}")
    if not os.getenv("LANGFUSE_HOST"):
        print("[tracing] WARNING: LANGFUSE_HOST unset — the SDK will default to EU cloud.")
        print("[tracing]          If your project is US, auth fails silently. Set it in .env.")

    class _FakeTicket:
        ticket_id = "tkt_smoke"
        customer_id = "cust_smoke"
        ticket_type = "order_status"

    @observe(name="smoke-child")
    def _child(x: int) -> int:
        return x * 2

    @observe(name="smoke-parent")
    def _parent() -> int:
        return _child(21)

    # trace_context opens the root span; both decorated functions nest inside it.
    # current_trace_id() is read INSIDE the block — outside it the span has
    # closed and there is no active context left to ask.
    with trace_context(_FakeTicket()):
        result = _parent()
        trace_id = current_trace_id()

    flush()
    print(f"[tracing] child returned {result}")
    print(f"[tracing] trace_id={trace_id}")
    if trace_id:
        print("[tracing] expect a 3-level tree: ticket:order_status > smoke-parent > smoke-child")
    else:
        print("[tracing] trace_id is None — no active span; the root span did not open.")
