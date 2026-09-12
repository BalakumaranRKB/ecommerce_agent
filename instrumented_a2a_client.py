"""
Watch the RAW StreamResponse chunks arrive from the specialist, one at a time,
instead of the final DisputeResult a2a_client.py extracts for you. This shows
the actual A2A wire behavior underneath the abstraction.

Requires: capabilities=AgentCapabilities(streaming=True) on the server's
AgentCard (already added to billing_specialist/a2a_server.py) -- without it,
Client.send_message() silently falls back to one buffered chunk instead of
three incremental ones.

    uv run python instrumented_a2a_client.py
"""

from __future__ import annotations

import asyncio
import uuid

from a2a.client import create_client, ClientConfig
from a2a.types import Message, Part, Role, SendMessageRequest
from a2a.utils.constants import TransportProtocol

from billing_specialist.a2a_server import build_agent_card
from billing_specialist.schemas import DisputeRequest

_CLIENT_CONFIG = ClientConfig(supported_protocol_bindings=[TransportProtocol.HTTP_JSON])


async def main():
    request = DisputeRequest(
        order_id="ord_6002",
        customer_id="cust_2002",
        refund_amount=8990,
        reason="Wrong model shipped",
        account_standing="flagged",
    )
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[Part(text=request.to_json())],
    )
    send_request = SendMessageRequest(message=message)

    client = await create_client(build_agent_card(), client_config=_CLIENT_CONFIG)

    print("Iterating client.send_message(...) -- watch each chunk as it arrives:\n")
    i = 0
    async for response in client.send_message(send_request):
        i += 1
        kind = response.WhichOneof("payload")
        print(f"--- StreamResponse #{i} --- payload = '{kind}'")
        if kind == "task":
            print(f"    task.status.state = {response.task.status.state}")
            print(f"    task.artifacts count = {len(response.task.artifacts)}")
        elif kind == "artifact_update":
            print(f"    artifact.name = {response.artifact_update.artifact.name}")
            text = response.artifact_update.artifact.parts[0].text
            print(f"    artifact.parts[0].text = {text[:90]}...")
        elif kind == "status_update":
            print(f"    status.state = {response.status_update.status.state}")
        elif kind == "message":
            print(f"    message.parts[0].text = {response.message.parts[0].text[:90]}")
        print()

    print(f"Total StreamResponse chunks received: {i}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
