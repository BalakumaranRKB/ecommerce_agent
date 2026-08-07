"""
MCP server (FastMCP, stdio transport) exposing the two read-only tools:
  - lookup_order(order_id)
  - check_account_status(customer_id)

Two protections live here, and they are different in kind:

1. Schema validation — FREE. FastMCP derives a JSON schema from each tool's type
   hints and validates every call against it before the function body runs. A
   malformed call (missing/wrong-typed field) is rejected by the protocol layer,
   not by an if-check we wrote.

2. Permission scoping — WRITTEN by us. The server is launched scoped to one
   ticket's customer, passed in via the TICKET_CUSTOMER_ID environment variable
   at process start (the client sets it). Any call for an order/customer that
   does not belong to that customer is rejected with a ToolError. This is the
   SECONDARY enforcement layer; the harness is the primary boundary (Stage 3).
   It exists so that no unscoped path is ever reachable, even if the server were
   somehow called directly.

Launched as a subprocess over stdio by mcp_client.py — not run by hand.

Assignment 2: reads Postgres via db.py instead of the in-memory mock dicts. The
scoping logic above is UNCHANGED — only the lookup moved. Each subprocess opens
its own short-lived connection, which is the honest cost of the per-call stdio
design carried over from Assignment 1.
"""

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

import db

# The customer this server instance is scoped to, set at launch by the client.
TICKET_CUSTOMER_ID = os.environ.get("TICKET_CUSTOMER_ID")

mcp = FastMCP("ecommerce-order-support", log_level="WARNING")


def _require_scope() -> str:
    """Fail closed: refuse to serve if the server was launched without a scope."""
    if not TICKET_CUSTOMER_ID:
        raise ToolError(
            "Server launched without TICKET_CUSTOMER_ID; refusing all calls "
            "(fail-closed — there is no unscoped mode)."
        )
    return TICKET_CUSTOMER_ID


@mcp.tool()
def lookup_order(order_id: str) -> dict:
    """Look up an order's status and contents by order ID."""
    scope = _require_scope()
    order = db.fetch_order(order_id)
    if not order:
        raise ToolError(f"No such order '{order_id}'.")
    if order["customer_id"] != scope:
        raise ToolError(
            f"PermissionError: order '{order_id}' does not belong to customer "
            f"'{scope}' on this ticket. Request rejected."
        )
    return order


@mcp.tool()
def check_account_status(customer_id: str) -> dict:
    """Look up a customer account's standing by customer ID."""
    scope = _require_scope()
    if customer_id != scope:
        raise ToolError(
            f"PermissionError: customer '{customer_id}' does not match customer "
            f"'{scope}' on this ticket. Request rejected."
        )
    account = db.fetch_account(customer_id)
    if not account:
        raise ToolError(f"No such account '{customer_id}'.")
    return account


if __name__ == "__main__":
    mcp.run(transport="stdio")
