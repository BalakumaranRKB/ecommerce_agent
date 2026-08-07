"""
Trajectory recording — the ordered list of steps the agent actually took.

THIS IS NOT TRACING. LangFuse spans and this trajectory record the same events
for two different audiences, and keeping them separate is deliberate
(docs/PLAN-assignment-2.md §5.4):

  LangFuse   -> a human clicks into it. Pretty timeline, nested spans, latency.
  Trajectory -> code loops over it. A plain Python list the eval can assert on.

The eval must run in CI where there is no LangFuse instance, and it must give
the same verdict every time. Querying a tracing backend to score a build would
couple the gate to a network service, its retention policy, and its API — and a
gate that fails when a dashboard is down is worse than no gate.

HOW STEPS GET RECORDED
Steps happen in three different modules (retriever, harness, mcp dispatch), so
threading a list through every call signature would mean touching a lot of code
that has nothing to do with evaluation. Instead the active trajectory lives in a
ContextVar — the same ambient-context pattern LangFuse itself uses. Modules call
record(...) and neither know nor care whether anyone is listening.

When nothing is recording, record() returns immediately. Normal CLI and server
operation pays nothing.

WHY REJECTIONS ARE STEPS
A harness rejection is a first-class event, not an error. The eval needs to tell
"never attempted a cross-customer lookup" apart from "attempted it and was
blocked" — those are very different agents, and only one of them is safe. An
eval that scored final answers could not distinguish them at all.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Iterator, Literal

StepKind = Literal["retrieval", "tool_call", "harness_reject", "memory", "answer"]


@dataclass
class TrajectoryStep:
    """One thing the agent did, recorded at the moment it happened."""

    kind: StepKind
    name: str
    args: dict = field(default_factory=dict)
    outcome: str = "ok"          # ok | empty | rejected | error
    detail: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trajectory:
    """The ordered steps of one turn, plus helpers the eval asserts against."""

    steps: list[TrajectoryStep] = field(default_factory=list)

    def append(self, step: TrajectoryStep) -> None:
        self.steps.append(step)

    def __len__(self) -> int:
        return len(self.steps)

    def __iter__(self) -> Iterator[TrajectoryStep]:
        return iter(self.steps)

    # -- queries the fixture rules are written in terms of --------------------

    def tools_called(self) -> list[str]:
        """Tool names that were actually DISPATCHED. A rejected proposal is not
        in here — that is the entire point of separating the two kinds."""
        return [s.name for s in self.steps if s.kind == "tool_call"]

    def rejections(self) -> list[TrajectoryStep]:
        return [s for s in self.steps if s.kind == "harness_reject"]

    def retrievals(self) -> list[TrajectoryStep]:
        return [s for s in self.steps if s.kind == "retrieval"]

    def retrieved_doc_ids(self) -> list[str]:
        """Every doc_id that came back from any retrieval this turn."""
        out: list[str] = []
        for step in self.retrievals():
            out.extend(step.detail.get("doc_ids", []))
        return out

    def retrieval_was_empty(self) -> bool:
        """True when retrieval ran and found nothing above the threshold.

        Distinct from 'retrieval never ran' — the honest-gap fixture needs the
        agent to have LOOKED and come up empty, not to have skipped the step.
        """
        rs = self.retrievals()
        return bool(rs) and all(s.outcome == "empty" for s in rs)

    def to_list(self) -> list[dict]:
        return [s.to_dict() for s in self.steps]

    def summary(self) -> str:
        """One line per step, for failure output that explains itself."""
        if not self.steps:
            return "    (no steps recorded)"
        lines = []
        for s in self.steps:
            bits = f"    {s.kind:<15} {s.name}"
            if s.kind == "retrieval":
                docs = s.detail.get("doc_ids", [])
                sims = s.detail.get("similarities", [])
                pairs = ", ".join(f"{d} ({v:.2f})" for d, v in zip(docs, sims)) or "nothing"
                bits += f"  -> {pairs}"
            elif s.kind == "tool_call":
                bits += f"  {s.args}"
            elif s.kind == "harness_reject":
                bits += f"  {s.args}  reason: {s.detail.get('reason', '?')}"
            elif s.kind == "memory":
                bits += f"  -> {s.detail.get('n_prior_tickets', 0)} prior ticket(s)"
            lines.append(bits)
        return "\n".join(lines)


# --------------------------------------------------------------- the recorder

_current: ContextVar[Trajectory | None] = ContextVar("current_trajectory", default=None)


@contextmanager
def recording() -> Iterator[Trajectory]:
    """Collect every step recorded inside this block.

    Uses a token to restore the previous value rather than clearing it, so
    nesting is safe and one turn can never leak steps into another.
    """
    trajectory = Trajectory()
    token = _current.set(trajectory)
    try:
        yield trajectory
    finally:
        _current.reset(token)


def record(
    kind: StepKind,
    name: str,
    args: dict | None = None,
    outcome: str = "ok",
    detail: dict | None = None,
) -> None:
    """Append a step to the active trajectory, if anything is recording.

    A no-op otherwise, which is why instrumented modules can call this
    unconditionally without knowing whether an eval is running.
    """
    trajectory = _current.get()
    if trajectory is None:
        return
    trajectory.append(
        TrajectoryStep(
            kind=kind,
            name=name,
            args=args or {},
            outcome=outcome,
            detail=detail or {},
        )
    )


if __name__ == "__main__":
    # Offline demo — no agent, no API key. Shows the two things the eval cares
    # about most: a dispatched call and a blocked one look completely different.
    print("=== a normal turn ===")
    with recording() as traj:
        record("memory", "prior_tickets", detail={"n_prior_tickets": 3})
        record("retrieval", "retrieve",
               args={"query": "my parcel is late"},
               detail={"doc_ids": ["shipping-delay-compensation"], "similarities": [0.54]})
        record("tool_call", "lookup_order", args={"order_id": "ord_5001"})
    print(traj.summary())
    print(f"  tools_called()       -> {traj.tools_called()}")
    print(f"  retrieved_doc_ids()  -> {traj.retrieved_doc_ids()}")

    print("\n=== a blocked cross-customer attempt ===")
    with recording() as traj:
        record("harness_reject", "lookup_order",
               args={"order_id": "ord_6002"},
               outcome="rejected",
               detail={"reason": "cross-customer access blocked"})
    print(traj.summary())
    print(f"  tools_called()       -> {traj.tools_called()}   <- empty: nothing dispatched")
    print(f"  rejections()         -> {len(traj.rejections())} recorded")
    print("\n  The attempt is visible AND the dispatch is absent. A final-answer")
    print("  check could not tell this apart from an agent that never tried.")

    print("\n=== nothing recording ===")
    record("tool_call", "lookup_order", args={"order_id": "ord_5001"})
    print("  record() outside a recording() block is a no-op — no error, no cost.")
