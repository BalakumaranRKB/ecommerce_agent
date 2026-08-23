"""
Tool specs advertised to the model, rendered into each provider's wire format.

These mirror the tools exposed by mcp_server.py (lookup_order,
check_account_status) and MUST stay in sync with them by name and parameters.
Defined once as TOOL_SPECS, then rendered to Anthropic's `input_schema` shape
and Groq's OpenAI-compatible `function.parameters` shape, so the agent code
does not care which backend is running.

STAGE 7 DEMO LEVER (AGENT_REGRESSED):
Setting AGENT_REGRESSED=true withholds `lookup_order` from the model. This is
the one deliberate defect used to prove the CI gate blocks a real regression
(PLAN-assignment-2.md Stage 7). It is an environment variable rather than a
code branch on purpose: the regressed commit is a one-line diff, no test is
edited, and no logic is mangled — so the gate is shown catching a change in
AGENT BEHAVIOUR, not a change in what the tests assert.

What it breaks, and why the fourth one is the interesting case:
  order_status_basic          — requires a lookup_order dispatch
  order_status_delivered      — requires a lookup_order dispatch
  delivery_late_compensation  — requires a lookup_order dispatch
  security_cross_customer     — requires a harness REJECTION to have been
      recorded. With no tool to propose, the model never proposes the
      cross-customer call, so the harness never blocks anything. The agent
      then looks SAFER by every surface measure (zero cross-customer
      dispatches) while the enforcement path has silently stopped being
      exercised at all. That fixture's own note anticipated exactly this:
      "an agent that never proposed the call would also have no dispatch, and
      that is indistinguishable from safety without the rejection record."
      A metric that only counted bad dispatches would report this as fine.
"""

import os

# Off unless explicitly switched on. Read once at import, like any other
# configuration, so nothing downstream has to know the demo exists.
REGRESSED = os.environ.get("AGENT_REGRESSED", "false").lower() == "true"

TOOL_SPECS = [
    {
        "name": "lookup_order",
        "description": "Look up an order's status and contents by order ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID, e.g. 'ord_5001'.",
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "check_account_status",
        "description": "Look up a customer account's standing by customer ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "The customer ID, e.g. 'cust_1001'.",
                },
            },
            "required": ["customer_id"],
        },
    },
]

# Applied AFTER the specs are defined and BEFORE they are rendered, so both
# provider formats below are filtered by construction and neither one can drift
# out of sync with the other. Note this withholds the tool from the MODEL only:
# harness.py's PERMITTED_TOOLS and the MCP server are untouched, so this is a
# regression in what the agent reaches for, not a hole in the boundary itself.
if REGRESSED:
    TOOL_SPECS = [s for s in TOOL_SPECS if s["name"] != "lookup_order"]


def _to_anthropic_tools(specs):
    return [
        {"name": s["name"], "description": s["description"], "input_schema": s["parameters"]}
        for s in specs
    ]


def _to_groq_tools(specs):
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["parameters"],
            },
        }
        for s in specs
    ]


TOOLS_BY_PROVIDER = {
    "anthropic": _to_anthropic_tools(TOOL_SPECS),
    "groq": _to_groq_tools(TOOL_SPECS),
}
