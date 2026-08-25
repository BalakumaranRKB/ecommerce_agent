"""
A2A client — sends dispute handoffs to the billing specialist service.

This is the main agent's side of the A2A boundary. It constructs a
SendMessageRequest with a DisputeRequest JSON Part, sends it to the specialist
running on port 5001, and extracts the DisputeResult from the response.

The client is deliberately thin: it serializes, sends, deserializes, and returns.
All decision logic lives in the specialist; all customer-facing phrasing lives
in the main agent's LLM.

    # Used internally by agent.py — not a standalone script.
"""

from __future__ import annotations

import json
import uuid

from a2a.client import create_client, ClientConfig
from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageRequest,
)
from a2a.utils.constants import TransportProtocol

from billing_specialist.a2a_server import build_agent_card
from billing_specialist.schemas import DisputeRequest, DisputeResult

SPECIALIST_URL = "http://localhost:5001"

# We hand the client an AgentCard directly instead of a bare URL string.
# Why: create_client(url) tries to DISCOVER the agent by fetching
# `<url>/.well-known/agent-card.json` first -- a real A2A protocol feature
# for finding agents you don't already know about. Our server doesn't (yet)
# serve that well-known path, and since both processes are part of the same
# project and already know each other, discovery is unnecessary overhead
# here. Passing the AgentCard directly skips the HTTP lookup and goes
# straight to the address in its `supported_interfaces`.
#
# The ClientConfig is required too: by default the client only tries the
# JSONRPC transport, but our server's create_rest_routes() speaks the
# HTTP_JSON (REST) transport. Both sides must agree on the binding or
# connection fails with "no compatible transports found" -- verified by
# reproducing that exact error against the real a2a-sdk 1.1.2 before writing
# this fix.
_CLIENT_CONFIG = ClientConfig(supported_protocol_bindings=[TransportProtocol.HTTP_JSON])


async def send_dispute_to_specialist(
    request: DisputeRequest,
) -> DisputeResult:
    """Send a DisputeRequest to the billing specialist via A2A and return
    the typed DisputeResult.

    This is an async function because the a2a-sdk client is async.
    The caller (agent.py) runs it with asyncio.run() from the sync context.
    """
    # Build the A2A message with the dispute request as a JSON part.
    # message_id is REQUIRED by the server's REST validation layer (not
    # optional despite proto3 not enforcing it at construction time) --
    # every A2A message needs a unique id for protocol-level tracking
    # (idempotency, task referencing). Omitting it produces a 400 from the
    # server before the request ever reaches our executor; confirmed by
    # running a live round-trip against the real server.
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[
            Part(text=request.to_json()),
        ],
    )

    send_request = SendMessageRequest(message=message)

    # Create client from the specialist's AgentCard directly (skips HTTP
    # discovery — see the module-level comment on _CLIENT_CONFIG) and force
    # the HTTP_JSON transport to match the server's REST routes.
    client = await create_client(build_agent_card(), client_config=_CLIENT_CONFIG)
    try:
        result_data = None
        async for response in client.send_message(send_request):
            # The response is a StreamResponse with task/message/status_update/artifact_update
            # We look for the artifact containing the DisputeResult
            if response.artifact_update is not None:
                artifact = response.artifact_update.artifact
                if artifact and artifact.parts:
                    for part in artifact.parts:
                        if part.text:
                            try:
                                data = json.loads(part.text)
                                if "order_id" in data and "action" in data:
                                    result_data = part.text
                            except (json.JSONDecodeError, TypeError):
                                continue

            # Also check if the response has a task with artifacts
            if response.task is not None and response.task.artifacts:
                for artifact in response.task.artifacts:
                    if artifact.parts:
                        for part in artifact.parts:
                            if part.text:
                                try:
                                    data = json.loads(part.text)
                                    if "order_id" in data and "action" in data:
                                        result_data = part.text
                                except (json.JSONDecodeError, TypeError):
                                    continue

        if result_data is None:
            raise RuntimeError(
                f"No DisputeResult received from specialist for "
                f"order {request.order_id}"
            )

        return DisputeResult.from_json(result_data)

    finally:
        await client.close()


def send_dispute_sync(request: DisputeRequest) -> DisputeResult:
    """Synchronous wrapper around send_dispute_to_specialist.

    The main agent's Session.answer_turn() is synchronous, so this bridge
    lets it call the async A2A client without restructuring the harness loop.
    """
    import asyncio

    # Handle the case where an event loop is already running
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        # We're inside an async context — use a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(
                asyncio.run, send_dispute_to_specialist(request)
            ).result()
    else:
        return asyncio.run(send_dispute_to_specialist(request))
