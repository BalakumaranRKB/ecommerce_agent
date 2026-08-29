"""
The human-in-the-loop approval gate — pause / resume state machine
(Phase 5 / spec §2.5, SPEC-HITL).

WHAT THIS IS
------------
When the policy boundary (`policy.py`) says a refund's amount `needs_approval`,
the refund does NOT execute. This gate:
  1. PAUSES — persists a real snapshot (`decisions.PendingApproval`) of what it
     was about to do into a durable store, and stops.
  2. RESUMES — on a human's approve/reject, re-fetches LIVE state, refuses to
     proceed if anything drifted since the pause (the §5.3 re-validation, the
     single most important safety property here), and only then executes.

THE ONE LEVER THAT MATTERS: correlate by RESOURCE, not by TICKET (§5.4)
----------------------------------------------------------------------
`req_r004` and `req_r005` BOTH target `ord_6002`. A gate keyed on the request
(ticket) id treats them as two independent approvals and executes two refunds on
one order — the §2.5 "confident wrong path." A gate keyed on the order (the
resource being mutated) sees the second as the same in-flight/executed action.

This whole module is built so that key choice is a SINGLE switch,
`correlate_by`, threaded through EVERY operation (create, resume, the executed
ledger). `correlate_by="request"` reproduces the bug; `correlate_by="order"`
(the default) is the fix. Same idiom as the Phase 1 CWP's `DROP_STANDING_IN_HANDOFF`
env toggle: broken and fixed run against the SAME code so they compare side by
side. Written up in `docs/hitl-bug-found.md`.

STORAGE
-------
`ApprovalStore` is an interface. `InMemoryApprovalStore` backs the tests and the
local build (no AWS). `AgentCoreMemoryApprovalStore` is the live backend on
AgentCore Memory (`MemorySessionManager`) — it needs a real `memory_id` and is
marked as unverified until run against a live resource. The gate logic does not
care which store it holds.

    uv run python -m hitl        # runs the broken-vs-fixed double-refund demo
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal, Optional

from decisions import EscalationReason, PendingApproval, RefundDecision


# ---------------------------------------------------------------------------
# State machine statuses (§5.5) — an explicit enum, NOT a boolean.
# ---------------------------------------------------------------------------
class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    executed = "executed"


CorrelateBy = Literal["order", "request"]
HumanAction = Literal["approve", "reject"]


@dataclass
class LiveRefundState:
    """The live state `resume()` re-fetches to compare against the parked
    snapshot. Drift in either field aborts the resume with `state_changed`."""
    refund_amount: int
    order_status: str


@dataclass
class ApprovalRecord:
    """What actually gets persisted: the §5.1 snapshot plus the state-machine
    bookkeeping the snapshot itself doesn't carry (status, created_at, ids)."""
    key: str
    order_id: str
    request_id: str
    status: ApprovalStatus
    created_at: float
    snapshot: PendingApproval

    def to_json(self) -> str:
        return json.dumps({
            "key": self.key,
            "order_id": self.order_id,
            "request_id": self.request_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "snapshot": self.snapshot.model_dump(),
        })

    @classmethod
    def from_json(cls, raw: str) -> "ApprovalRecord":
        d = json.loads(raw)
        return cls(
            key=d["key"], order_id=d["order_id"], request_id=d["request_id"],
            status=ApprovalStatus(d["status"]), created_at=d["created_at"],
            snapshot=PendingApproval(**d["snapshot"]),
        )


@dataclass
class ResumeOutcome:
    """The structured result of a resume — never a bare bool. `escalation`
    carries the §5.3/§5.4 abort reason (`state_changed` / `double_refund`)."""
    executed: bool
    status: ApprovalStatus
    escalation: Optional[EscalationReason] = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Storage interface + two backends.
# ---------------------------------------------------------------------------
class ApprovalStore(ABC):
    @abstractmethod
    def put(self, record: ApprovalRecord) -> None: ...
    @abstractmethod
    def get(self, key: str) -> Optional[ApprovalRecord]: ...
    @abstractmethod
    def all(self) -> list[ApprovalRecord]: ...


class InMemoryApprovalStore(ApprovalStore):
    """Backs tests and the local build. No durability, no AWS."""
    def __init__(self) -> None:
        self._by_key: dict[str, ApprovalRecord] = {}

    def put(self, record: ApprovalRecord) -> None:
        self._by_key[record.key] = record

    def get(self, key: str) -> Optional[ApprovalRecord]:
        return self._by_key.get(key)

    def all(self) -> list[ApprovalRecord]:
        return list(self._by_key.values())


class AgentCoreMemoryApprovalStore(ApprovalStore):
    """Durable backend on AgentCore Memory (`MemorySessionManager`).

    Persists each record as a JSON blob event; reads them back by listing events.
    Correlation is by the record's `key`.

    Kept out of the default/test path deliberately: the gate logic is proven
    against InMemory; this only swaps WHERE records live for the live demo.
    """
    def __init__(self, memory_id: str, region_name: str = "ap-south-1",
                 actor_id: str = "ordercare-hitl", session_id: str = "refund-approvals"):
        from bedrock_agentcore.memory.session import MemorySessionManager  # lazy: no AWS at import
        self._mgr = MemorySessionManager(memory_id=memory_id, region_name=region_name)
        self._actor_id = actor_id
        self._session_id = session_id

    def put(self, record: ApprovalRecord) -> None:
        from bedrock_agentcore.memory.constants import BlobMessage
        blob_payload = json.dumps({"approval_record": record.to_json(), "key": record.key})
        self._mgr.add_turns(
            actor_id=self._actor_id, session_id=self._session_id,
            messages=[BlobMessage(blob_payload)],
        )

    def get(self, key: str) -> Optional[ApprovalRecord]:
        # AWS list_events returns newest events first (reverse-chronological).
        # First match is the latest record.
        events = self._mgr.list_events(actor_id=self._actor_id, session_id=self._session_id)
        for ev in events:
            payload = ev.get("payload") or []
            for item in payload:
                raw_blob = item.get("blob") if isinstance(item, dict) or hasattr(item, "get") else None
                if isinstance(raw_blob, str):
                    try:
                        data = json.loads(raw_blob)
                        if isinstance(data, dict) and data.get("key") == key:
                            return ApprovalRecord.from_json(data["approval_record"])
                    except Exception:
                        pass
                elif isinstance(raw_blob, dict):
                    if raw_blob.get("key") == key:
                        return ApprovalRecord.from_json(raw_blob["approval_record"])
        return None

    def all(self) -> list[ApprovalRecord]:
        # AWS list_events returns newest events first. Keep first match per key.
        events = self._mgr.list_events(actor_id=self._actor_id, session_id=self._session_id)
        out: dict[str, ApprovalRecord] = {}
        for ev in events:
            payload = ev.get("payload") or []
            for item in payload:
                raw_blob = item.get("blob") if isinstance(item, dict) or hasattr(item, "get") else None
                if isinstance(raw_blob, str):
                    try:
                        data = json.loads(raw_blob)
                        if isinstance(data, dict) and "approval_record" in data:
                            rec = ApprovalRecord.from_json(data["approval_record"])
                            if rec.key not in out:
                                out[rec.key] = rec
                    except Exception:
                        pass
                elif isinstance(raw_blob, dict):
                    if "approval_record" in raw_blob:
                        rec = ApprovalRecord.from_json(raw_blob["approval_record"])
                        if rec.key not in out:
                            out[rec.key] = rec
        return list(out.values())


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------
def _default_refetch(order_id: str, request_id: str) -> LiveRefundState:
    """Read live order/refund state from the same mock_data the specialist
    reads. Tests inject their own refetch to simulate drift."""
    from mock_data import ORDERS, REFUND_REQUESTS
    order = ORDERS.get(order_id)
    req = REFUND_REQUESTS.get(request_id)
    return LiveRefundState(
        refund_amount=req["amount"] if req else -1,
        order_status=order["status"] if order else "unknown",
    )


class HITLGate:
    """Approval gate. Persists pauses, resumes with re-validation, expires stale
    approvals, and correlates by resource (order) — or by ticket (request), the
    seeded bug — depending on `correlate_by`."""

    def __init__(
        self,
        store: Optional[ApprovalStore] = None,
        correlate_by: CorrelateBy = "order",
        ttl_seconds: float = 24 * 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store or InMemoryApprovalStore()
        self.correlate_by = correlate_by
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        # Executed ledger, keyed by the SAME correlation key. In order-mode this
        # is per-order (correct); in request-mode it is per-request (the bug:
        # useless for spotting a second refund on the same order).
        self._executed_keys: set[str] = set()

    def _key(self, order_id: str, request_id: str) -> str:
        return order_id if self.correlate_by == "order" else request_id

    # -- Pause (§5.1 / §5.2) ------------------------------------------------
    def request_approval(
        self, decision: RefundDecision, request_id: str, order_status: str
    ) -> PendingApproval:
        """Halt and persist a snapshot. Idempotent per correlation key: a second
        request on the SAME key (e.g. req_r005 on ord_6002 in order-mode) returns
        the existing pending rather than creating a duplicate — resource
        correlation starting at creation time."""
        key = self._key(decision.order_id, request_id)
        existing = self.store.get(key)
        if existing is not None and existing.status == ApprovalStatus.pending:
            return existing.snapshot  # already parked for this resource

        snapshot = PendingApproval(
            order_id=decision.order_id,
            customer_id=decision.customer_id,
            amount=decision.amount,
            status_snapshot=order_status,
            requested_refund=decision.amount,
            reasoning=decision.reasoning,
        )
        self.store.put(ApprovalRecord(
            key=key, order_id=decision.order_id, request_id=request_id,
            status=ApprovalStatus.pending, created_at=self.clock(), snapshot=snapshot,
        ))
        return snapshot

    # -- Resume (§5.2 / §5.3 / §5.4 / §5.5) ---------------------------------
    def resume(
        self,
        order_id: str,
        request_id: str,
        human_action: HumanAction,
        refetch: Callable[[str, str], LiveRefundState] = _default_refetch,
    ) -> ResumeOutcome:
        """Resume a paused approval. Order of checks matters:
        expiry → re-validate live state → double-refund guard → act."""
        key = self._key(order_id, request_id)
        rec = self.store.get(key)
        if rec is None:
            return ResumeOutcome(False, ApprovalStatus.rejected,
                                 detail=f"no pending approval for key {key!r}")

        # Already-terminal (executed/expired/rejected) → do not act again.
        if rec.status == ApprovalStatus.executed or key in self._executed_keys:
            return ResumeOutcome(
                False, ApprovalStatus.executed,
                escalation=EscalationReason(
                    code="double_refund",
                    detail=f"Order {order_id} already has an executed refund."),
                detail="refused: double refund on the same resource")
        if rec.status == ApprovalStatus.expired:
            return ResumeOutcome(False, ApprovalStatus.expired, detail="already expired")

        # §5.5 expiry: a pending that sat past the window cannot execute.
        if self.clock() - rec.created_at > self.ttl_seconds:
            rec.status = ApprovalStatus.expired
            self.store.put(rec)
            return ResumeOutcome(False, ApprovalStatus.expired,
                                 detail=f"approval expired after {self.ttl_seconds}s")

        if human_action == "reject":
            rec.status = ApprovalStatus.rejected
            self.store.put(rec)
            return ResumeOutcome(False, ApprovalStatus.rejected, detail="rejected by human")

        # §5.3 re-validation: re-fetch live state, abort on ANY drift.
        live = refetch(order_id, request_id)
        if (live.refund_amount != rec.snapshot.requested_refund
                or live.order_status != rec.snapshot.status_snapshot):
            return ResumeOutcome(
                False, ApprovalStatus.pending,
                escalation=EscalationReason(
                    code="state_changed",
                    detail=(f"State drifted since approval was requested: "
                            f"amount {rec.snapshot.requested_refund}->{live.refund_amount}, "
                            f"status {rec.snapshot.status_snapshot!r}->{live.order_status!r}.")),
                detail="refused: live state changed under the pause")

        # §5.4 resource-correlation guard (redundant with the terminal check
        # above, kept explicit): does THIS resource already have a refund out?
        if key in self._executed_keys:
            return ResumeOutcome(
                False, ApprovalStatus.executed,
                escalation=EscalationReason(
                    code="double_refund",
                    detail=f"Order {order_id} already has an executed refund."),
                detail="refused: double refund on the same resource")

        # All checks passed → execute exactly once.
        rec.status = ApprovalStatus.executed
        self.store.put(rec)
        self._executed_keys.add(key)
        return ResumeOutcome(True, ApprovalStatus.executed, detail="refund executed")


# ---------------------------------------------------------------------------
# Standalone demo — the double-refund CWP, broken vs fixed, no AWS/API key.
#   uv run python -m hitl
# ---------------------------------------------------------------------------
def _demo_double_refund(correlate_by: CorrelateBy) -> int:
    """Drive req_r004 then req_r005 (both ord_6002) through the gate; return the
    number of refunds that executed."""
    from mock_data import REFUND_REQUESTS
    gate = HITLGate(correlate_by=correlate_by)
    executed = 0
    for rid in ("req_r004", "req_r005"):
        r = REFUND_REQUESTS[rid]
        decision = RefundDecision(
            order_id=r["order_id"], customer_id=r["customer_id"], amount=r["amount"],
            action="escalate_hitl", requires_human=True, reasoning=r["reason"],
        )
        gate.request_approval(decision, request_id=rid, order_status="delivered")
    for rid in ("req_r004", "req_r005"):
        r = REFUND_REQUESTS[rid]
        outcome = gate.resume(r["order_id"], rid, "approve")
        if outcome.executed:
            executed += 1
    return executed


if __name__ == "__main__":
    print("=== HITL double-refund CWP (both req_r004 + req_r005 target ord_6002) ===\n")
    broken = _demo_double_refund("request")
    fixed = _demo_double_refund("order")
    print(f"  correlate_by='request' (ticket-keyed, THE BUG): {broken} refunds executed")
    print(f"  correlate_by='order'   (resource-keyed, FIXED): {fixed} refunds executed")
    print(f"\n  {'BUG reproduced' if broken == 2 else '??'}: ticket-keying pays out twice on one order.")
    print(f"  {'FIX confirmed' if fixed == 1 else '??'}: resource-keying pays out once.")
