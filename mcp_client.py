"""
Client wrapper the harness uses to call the MCP tools.

Exposes ONE simple synchronous function, call_tool(), so the (sync) harness in
harness.py never has to touch asyncio. Each call launches mcp_server.py as a
subprocess over stdio, scoped to the given customer via TICKET_CUSTOMER_ID, runs
the call, and tears the subprocess down.

Design note: launching the server per call (rather than holding one long-lived
session) keeps this module tiny and robust — the whole async lifecycle lives
inside a single task, so there are no event-loop or cancel-scope pitfalls. For
a support conversation with a handful of tool calls that trade-off is invisible;
a persistent session would be the optimization if call volume ever mattered.

Run the Stage 2 acceptance demo (launches the real server, makes real calls):
    uv run python mcp_client.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# Resolve the server script next to this file, so it works regardless of CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SERVER = os.path.join(_HERE, "mcp_server.py")


def call_tool(
    customer_id: str,
    name: str,
    arguments: dict,
    server_script: str = DEFAULT_SERVER,
) -> tuple[bool, str]:
    """Call an MCP tool on a server scoped to `customer_id`.

    This is the public, SYNCHRONOUS entry point the harness uses. The MCP SDK is
    async and our harness is a plain sync while-loop; this function is the bridge
    between the two.

    Returns (is_error, text). is_error is True for schema-validation failures,
    scope rejections, and any transport error; text carries the message.
    """
    # asyncio.run() spins up a fresh event loop, runs the async coroutine below
    # to completion, hands back its return value, then tears the loop down. To
    # the caller this behaves like an ordinary blocking function — no async
    # leaks out into the harness.
    return asyncio.run(_call_once(customer_id, name, arguments, server_script))


async def _call_once(customer_id, name, arguments, server_script):
    # 1. Build the launch parameters: how to start the server subprocess.
    params = StdioServerParameters(
        command=sys.executable,          # reuse THIS Python (it has mcp installed)
        args=[server_script],            # ...to run mcp_server.py
        # Copy our environment and inject the scope. THIS is how the server gets
        # scoped: the client sets TICKET_CUSTOMER_ID, the server reads it at
        # startup. The server is never wider than the customer we pass here.
        env={**os.environ, "TICKET_CUSTOMER_ID": customer_id},
    )
    try:
        # 2. Launch the server subprocess and open the stdio pipes.
        #      read  = bytes coming FROM the server's stdout
        #      write = bytes going TO the server's stdin
        #    ("stdio transport" just means we talk over standard in/out.) Exiting
        #    this `async with` — on return OR on error — shuts the subprocess
        #    down automatically, so there is no manual cleanup.
        async with stdio_client(params) as (read, write):
            # 3. Wrap the raw pipes in a protocol-aware session, then do the
            #    required MCP handshake. initialize() must run before any tool
            #    call — it is where both sides exchange capabilities.
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 4. Make the actual call. On the server this triggers schema
                #    validation (free) + the scope check, then the tool runs or
                #    raises. Back comes a result with .content and .isError.
                result = await session.call_tool(name, arguments)

                # 5. Flatten the content blocks into one string. Each block `b`
                #    normally has a .text attribute; fall back to str(b) if not.
                text = "\n".join(
                    getattr(b, "text", str(b)) for b in result.content
                )
                return bool(result.isError), text
    except Exception as e:
        # 6. Any TRANSPORT-level failure (server won't start, pipe breaks, SDK
        #    raises client-side) becomes the same (is_error, text) shape as a
        #    normal rejection — so the harness never crashes and always receives
        #    one consistent result shape.
        return True, f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    # Stage 2 acceptance: a server scoped to cust_1001, exercised live.
    SCOPE = "cust_1001"
    print(f"[MCP DEMO] server scoped to ticket customer: {SCOPE}\n")

    checks = [
        ("malformed (missing order_id)   ", "lookup_order",        {}),
        ("cross-customer order (ord_6002)", "lookup_order",        {"order_id": "ord_6002"}),
        ("own order (ord_5001)           ", "lookup_order",        {"order_id": "ord_5001"}),
        ("own account (cust_1001)        ", "check_account_status", {"customer_id": "cust_1001"}),
        ("cross-customer acct (cust_2002)", "check_account_status", {"customer_id": "cust_2002"}),
    ]

    for label, name, args in checks:
        is_error, text = call_tool(SCOPE, name, args)
        tag = "REJECTED" if is_error else "ok      "
        print(f"  {label} -> {tag}: {text}")
