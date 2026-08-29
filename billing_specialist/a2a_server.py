"""
A2A HTTP server for the billing specialist — separate process on port 5001.

This wraps the deterministic `resolve_dispute()` function behind the A2A protocol
using `a2a-sdk[http-server]`. The same function is also callable as a plain Python
function for standalone tests — same function, two entry points (spec §1.3).

Protocol: the main agent sends a Message with a JSON Part containing a
DisputeRequest. The specialist resolves it and returns a Task with an Artifact
containing the DisputeResult as a JSON Part.

    uv run python -m billing_specialist.a2a_server
"""

from __future__ import annotations

import asyncio
import json
import sys
import os

# Allow imports from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uvicorn
from starlette.applications import Starlette

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
)
from a2a.utils.constants import TransportProtocol

from billing_specialist.agent import resolve_dispute
from billing_specialist.settlement import settle_dispute, resume_settlement
from billing_specialist.schemas import DisputeRequest, DisputeResult


SPECIALIST_PORT = 5001


class BillingSpecialistExecutor(AgentExecutor):
    """A2A executor that wraps resolve_dispute().

    The executor receives a DisputeRequest as a JSON Part in the incoming
    Message, runs the deterministic dispute logic, and publishes the result
    as a Task with an Artifact containing the DisputeResult.
    """

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # Extract the incoming message from the request.
        # NOTE: a2a-sdk 1.1.2's RequestContext exposes `.message`, `.task_id`,
        # `.context_id` as plain attributes -- there is no get_user_request() /
        # get_task_id() / get_context_id(). (Confirmed against the installed
        # package; the earlier draft assumed a getter-method API that does not
        # exist in this version.)
        message = context.message
        if message is None:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_FAILED,
                        message=Message(
                            role=Role.ROLE_AGENT,
                            parts=[Part(text="No message received in request.")],
                        ),
                    )
                )
            )
            return

        # Find the JSON part carrying either a DisputeRequest (settle) or a
        # resume instruction ({"op":"resume", ...}). Both carry "order_id".
        payload_json = None
        for part in message.parts:
            if part.text:
                try:
                    data = json.loads(part.text)
                    if isinstance(data, dict) and "order_id" in data:
                        payload_json = part.text
                        break
                except (json.JSONDecodeError, TypeError):
                    continue

        if payload_json is None:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_FAILED,
                        message=Message(
                            role=Role.ROLE_AGENT,
                            parts=[Part(text="No DisputeRequest or resume instruction found in message parts.")],
                        ),
                    )
                )
            )
            return

        # Parse, run the right operation, and return.
        # Phase 5: the executor runs the specialist's SETTLEMENT seam, not just
        # the verdict -- settle_dispute() decides, authorizes the amount, then
        # executes or pauses; resume_settlement() actions a human's approve/reject
        # on a paused refund. The verdict fields (action/approved/hitl) in the
        # returned DisputeResult are unchanged from resolve_dispute(), so the
        # existing round-trip contract still holds; settlement_outcome is added.
        try:
            data = json.loads(payload_json)
            if data.get("op") == "resume":
                order_id = data["order_id"]
                human_action = data.get("human_action", "approve")
                print(f"  [SPECIALIST] Resume: {order_id} / human_action={human_action}")
                result = resume_settlement(order_id, human_action)
                artifact_order_id = order_id
            else:
                dispute_request = DisputeRequest.from_json(payload_json)
                print(f"  [SPECIALIST] Received dispute: {dispute_request.order_id} / "
                      f"{dispute_request.customer_id} / INR {dispute_request.refund_amount:,} / "
                      f"standing={dispute_request.account_standing}")
                result = settle_dispute(dispute_request)
                artifact_order_id = dispute_request.order_id

            print(f"  [SPECIALIST] Decision: {result.action} "
                  f"(approved={result.approved}, hitl={result.requires_human_review}, "
                  f"settlement={result.settlement_outcome})")

        except Exception as e:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_FAILED,
                        message=Message(
                            role=Role.ROLE_AGENT,
                            parts=[Part(text=f"Error processing dispute: {e}")],
                        ),
                    )
                )
            )
            return

        # Emit the Task with a working status, then the artifact, then completed
        task_id = context.task_id
        context_id = context.context_id

        # Create a Task object first to register it with the framework
        await event_queue.enqueue_event(
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
        )

        # Publish the result as an artifact.
        # NOTE: task_id/context_id must be set here too -- proto3 silently
        # accepts them missing (defaults to ""), but the server's TaskManager
        # validates the event's task_id against the task it's tracking and
        # rejects a mismatch at runtime ("Task in event doesn't match
        # TaskManager <id>"). Only found by running a live round-trip --
        # nothing at import or construction time catches this.
        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                artifact=Artifact(
                    artifact_id=f"dispute-result-{artifact_order_id}",
                    name="DisputeResult",
                    description=f"Dispute resolution for order {artifact_order_id}",
                    parts=[Part(text=result.to_json())],
                ),
            )
        )

        # Mark as completed -- same requirement: task_id/context_id must match.
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_COMPLETED,
                    message=Message(
                        role=Role.ROLE_AGENT,
                        parts=[Part(text=result.reasoning)],
                    ),
                ),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Handle cancellation — mark the task as cancelled."""
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                status=TaskStatus(
                    state=TaskState.TASK_STATE_CANCELED,
                    message=Message(
                        role=Role.ROLE_AGENT,
                        parts=[Part(text="Dispute resolution cancelled.")],
                    ),
                ),
            )
        )


def build_agent_card() -> AgentCard:
    """Build the A2A Agent Card for the billing specialist.

    `supported_interfaces` is what tells a client WHERE this agent lives and
    WHICH transport to speak. Without it, an AgentCard is just a description
    with no address -- a client handed this card would have nothing to
    connect to. (Discovered by testing: `create_client()` given a bare
    AgentCard skips the `.well-known/agent-card.json` HTTP lookup entirely
    and goes straight off `supported_interfaces` instead.)
    """
    return AgentCard(
        name="Billing Specialist",
        description=(
            "Resolves billing disputes and processes refund decisions. "
            "Accepts a DisputeRequest (order_id, customer_id, refund_amount, "
            "reason, account_standing) and returns a typed DisputeResult."
        ),
        version="1.0.0",
        # Without this, capabilities.streaming defaults to False and
        # Client.send_message() silently falls back to one-shot aggregation
        # even though ClientConfig asks for streaming -- verified live: the
        # server logged POST /message:send (buffered, 1 chunk) without this
        # field, and POST /message:stream (3 real incremental chunks, one
        # per enqueue_event call below) once it was added.
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                url=f"http://localhost:{SPECIALIST_PORT}",
                # create_rest_routes() serves REST-style paths
                # (/message:send, /tasks/{id}, ...) -- that IS the HTTP_JSON
                # transport, not JSONRPC (a single-endpoint RPC style). The
                # client must be configured for the same binding or
                # connection fails with "no compatible transports found".
                protocol_binding=TransportProtocol.HTTP_JSON,
            ),
        ],
        skills=[
            AgentSkill(
                id="resolve-dispute",
                name="Resolve Billing Dispute",
                description=(
                    "Evaluates a refund request against policy rules: amount threshold, "
                    "account standing, double-refund guard. Returns approve/reject/escalate."
                ),
            ),
        ],
    )


def create_app() -> Starlette:
    """Build the Starlette application with A2A routes."""
    agent_card = build_agent_card()
    executor = BillingSpecialistExecutor()
    task_store = InMemoryTaskStore()

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )

    routes = create_rest_routes(request_handler)
    return Starlette(routes=routes)


app = create_app()


if __name__ == "__main__":
    print(f"[billing-specialist] Starting A2A server on port {SPECIALIST_PORT}")
    print(f"[billing-specialist] Agent Card: {build_agent_card().name}")
    uvicorn.run(
        "billing_specialist.a2a_server:app",
        host="0.0.0.0",
        port=SPECIALIST_PORT,
        reload=False,
    )
