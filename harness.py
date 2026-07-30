"""
The harness — the single boundary between "the model proposes a tool call" and
"the tool call executes."

This is the most important pattern in the project: the harness (our code), not
the model, decides what runs. Point at harness_check() below — a proposed call
is inspected there and can be rejected BEFORE it is dispatched, and permission
is scoped to the active ticket's customer, so a cross-customer request never
reaches a tool.

Two layers of enforcement, in this order (see docs/PLAN.md §4):
  1. PRIMARY — harness_check(), here. Runs before dispatch. A rejected call is
     never sent to MCP at all.
  2. SECONDARY — the MCP server, launched scoped to the ticket customer, which
     re-checks every call it receives (mcp_server.py). This exists so that no
     unscoped path is reachable even if the server were called directly.

Stage 3 wired the real MCP tools in behind this boundary. Note what did NOT
change when they arrived: harness_check() is the same logic it enforced in
Stage 1 against stubs. The boundary was never bolted on afterwards.

Run the boundary demo (launches the real MCP server; no API key needed):
    uv run python harness.py
"""

from __future__ import annotations

from typing import Callable

import mock_data as data
from mcp_client import call_tool
from ticket import Ticket

# The only tools the model is ever allowed to invoke. Both are read-only.
PERMITTED_TOOLS = {"lookup_order", "check_account_status"}

# An ownership resolver answers "which customer owns the entity in this proposed
# call?" The harness needs its own answer to this BEFORE dispatch — it must never
# rely on the (scoped) tool to police itself. In production this would be an
# authorization service; here it reads the mock DB directly.
OwnerResolver = Callable[[str, dict], "str | None"]


def make_owner_resolver(order_owner) -> OwnerResolver:
    """Build an ownership resolver. `order_owner` maps order_id -> owning
    customer_id: a plain dict, or any callable with the same shape."""
    lookup = order_owner.get if isinstance(order_owner, dict) else order_owner

    def resolve_owner(tool_name: str, tool_input: dict):
        if tool_name == "lookup_order":
            return lookup(tool_input.get("order_id"))
        if tool_name == "check_account_status":
            # The queried customer IS the entity; its "owner" is itself.
            return tool_input.get("customer_id")
        return None

    return resolve_owner


def db_owner_resolver() -> OwnerResolver:
    """The real resolver, backed by the mock DB's ownership index."""
    return make_owner_resolver(data.ORDER_OWNER)


def harness_check(
    tool_name: str,
    tool_input: dict,
    ticket: Ticket,
    resolve_owner: OwnerResolver,
) -> tuple[bool, str]:
    """THE BOUNDARY. Returns (allowed, reason). If allowed is False, the caller
    must NOT dispatch the call.

    Two rules, checked here before anything runs:
      1. the tool must be in the permitted read-only set, and
      2. the requested entity must belong to the active ticket's customer.
    """
    if tool_name not in PERMITTED_TOOLS:
        return False, f"tool '{tool_name}' is not in the permitted set {sorted(PERMITTED_TOOLS)}"

    owner = resolve_owner(tool_name, tool_input)
    if owner is None:
        return False, f"unknown or malformed target for {tool_name}({tool_input})"
    if owner != ticket.customer_id:
        return False, (
            f"cross-customer access blocked: target belongs to '{owner}', "
            f"not ticket customer '{ticket.customer_id}'"
        )

    return True, "ok"


def make_mcp_dispatch(ticket: Ticket) -> Callable[[str, dict], str]:
    """Build the dispatcher that actually runs an APPROVED call.

    The ticket's customer_id is passed to the MCP client, which launches the
    server scoped to that customer — so the harness's check and the server's
    scope are anchored to the same customer by construction, not by convention.
    """

    def dispatch(tool_name: str, tool_input: dict) -> str:
        is_error, text = call_tool(ticket.customer_id, tool_name, tool_input)
        return f"TOOL ERROR: {text}" if is_error else text

    return dispatch


def run_harnessed(
    messages: list,
    ticket: Ticket,
    provider,
    tools: list,
    resolve_owner: OwnerResolver,
    dispatch: Callable[[str, dict], str],
    system_prompt: str,
) -> str:
    """The harness loop: the model proposes -> harness_check decides -> the
    harness dispatches or rejects -> the result is fed back -> repeat until the
    model stops proposing calls.

    `messages` is the LIVE conversation buffer (memory.Conversation.messages).
    The loop appends to it in place — assistant turns, tool results, and the
    final answer — so a later turn can still see what an earlier turn fetched.
    The caller appends the customer's message before calling.

    The rejection reason is fed back to the model as the tool result, so it can
    explain the refusal to the customer instead of silently stalling.
    """
    response = provider.create(system_prompt, messages, tools)

    while response.stop_reason == "tool_use":
        provider.append_assistant_turn(messages, response)

        results = {}
        for tc in response.tool_calls:
            # ---- THE BOUNDARY: nothing is dispatched until this returns True ----
            allowed, reason = harness_check(tc.name, tc.input, ticket, resolve_owner)
            if not allowed:
                print(f"  [HARNESS] REJECTED {tc.name}({tc.input})  ->  {reason}")
                results[tc.id] = f"BLOCKED BY HARNESS: {reason}"
            else:
                print(f"  [HARNESS] allowed  {tc.name}({tc.input})")
                results[tc.id] = dispatch(tc.name, tc.input)
            # --------------------------------------------------------------------

        provider.append_tool_results(messages, response, results)
        response = provider.create(system_prompt, messages, tools)

    # The final answer goes into the buffer too, so the next turn sees it.
    provider.append_assistant_turn(messages, response)
    return response.text


# ---------------------------------------------------------------------------
# Stage 3 demo — proves the harness is the PRIMARY boundary, using the real MCP
# tools and the real mock DB. Deterministic: the proposed calls are hard-coded
# exactly as a model would have proposed them, so no API key is needed.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ticket = Ticket(ticket_id="tkt_9001", customer_id="cust_1001", ticket_type="refund")
    resolve_owner = db_owner_resolver()
    dispatch = make_mcp_dispatch(ticket)

    # Count real MCP dispatches, to prove rejected calls never reached the tool.
    dispatched: list[str] = []

    def counting_dispatch(name, inp):
        dispatched.append(f"{name}({inp})")
        return dispatch(name, inp)

    proposed = [
        ("lookup_order",         {"order_id": "ord_5001"}),      # own order      -> allow
        ("lookup_order",         {"order_id": "ord_6002"}),      # OTHER customer -> reject
        ("check_account_status", {"customer_id": "cust_1001"}),  # own account    -> allow
        ("check_account_status", {"customer_id": "cust_2002"}),  # OTHER customer -> reject
        ("set_account_status",   {"customer_id": "cust_1001"}),  # not permitted  -> reject
    ]

    print(f"Active ticket: {ticket}\n")
    print("=== harnessed dispatch (harness_check decides BEFORE MCP is called) ===")
    for name, inp in proposed:
        allowed, reason = harness_check(name, inp, ticket, resolve_owner)
        if allowed:
            out = counting_dispatch(name, inp)
            print(f"  ALLOWED  {name}({inp})\n           -> {out}")
        else:
            print(f"  REJECTED {name}({inp})\n           -> {reason}")

    print("\n=== proof the boundary is primary, not a formality ===")
    print(f"  proposed calls       : {len(proposed)}")
    print(f"  actually reached MCP : {len(dispatched)}")
    for d in dispatched:
        print(f"      dispatched: {d}")
    print("  the 3 rejected calls never left this process.")
