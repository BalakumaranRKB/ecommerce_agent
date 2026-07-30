"""
Tool specs advertised to the model, rendered into each provider's wire format.

These mirror the tools exposed by mcp_server.py (lookup_order,
check_account_status) and MUST stay in sync with them by name and parameters.
Defined once as TOOL_SPECS, then rendered to Anthropic's `input_schema` shape
and Groq's OpenAI-compatible `function.parameters` shape, so the agent code
does not care which backend is running.
"""

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
