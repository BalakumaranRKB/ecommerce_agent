"""
Trajectory eval — scores the PATH an answer took, never the answer text.

This is the Stage 3 half: run every fixture, check its rules against the
recorded trajectory, print a pass rate with per-ticket reasons. Stage 6 adds the
baseline comparison and the non-zero exit that turns this into a gate.

WHY TRAJECTORY AND NOT ANSWER TEXT (docs/PLAN-assignment-2.md §4)
The assignment names this as the most common way to lose marks: a gate that only
checks the final answer rebuilds the confident-wrong-path blind spot one layer
up. We already have a live instance of it — the agent produces a well-written,
appropriately-hedged answer while having retrieved entirely the wrong policy
documents. An answer-text check passes that. A trajectory check catches it,
because the doc_ids are right there in the record.

WHY FAILURES PRINT THE WHOLE TRAJECTORY
A failure that says "expected shipping-delay-compensation" tells you what broke.
Printing the steps tells you why — which docs came back instead, with what
scores, and whether the tool call happened at all. The Stage 4 search for a
confident wrong path is done by reading this output, so it has to carry enough
detail to reason from.

    uv run python -m eval.regression_eval
    uv run python -m eval.regression_eval --only delivery_late_compensation
    uv run python -m eval.regression_eval --json results.json
    uv run python -m eval.regression_eval --write-baseline
    uv run python -m eval.regression_eval --baseline        # gate: exits 1 on a regression
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent import Session
from eval.trajectory import Trajectory
from llm import get_provider
from retriever import PolicyRetriever
from ticket import Ticket

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES_PATH = os.path.join(_HERE, "fixtures.json")
BASELINE_PATH = os.path.join(_HERE, "baseline.json")

# Default regression threshold in percentage points. On a 12-fixture suite one
# ticket is worth 8.3pp, so 5.0 sits below the value of a single ticket: any
# single-ticket regression trips the gate, while a threshold >=8.3 could not
# detect one at all. See PLAN-assignment-2.md §5.3.
DEFAULT_THRESHOLD_PP = 5.0


@dataclass
class TicketResult:
    id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    trajectory: Trajectory | None = None
    answer: str = ""
    elapsed: float = 0.0


def load_fixtures(path: str = FIXTURES_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["fixtures"]


def score(fixture: dict, traj: Trajectory) -> list[str]:
    """Check one fixture's rules against one trajectory.

    Returns a list of human-readable failure reasons — empty means passed.
    Every branch says what was expected AND what actually happened, because the
    reason is the whole value of a failing test.
    """
    requires = fixture.get("requires", {})
    forbids = fixture.get("forbids", {})
    failures: list[str] = []

    called = traj.tools_called()
    retrieved = traj.retrieved_doc_ids()

    for tool in requires.get("tools", []):
        if tool not in called:
            failures.append(f"required tool '{tool}' was never dispatched (dispatched: {called or 'nothing'})")

    for tool in forbids.get("tools", []):
        if tool in called:
            failures.append(f"forbidden tool '{tool}' WAS dispatched")

    if requires.get("retrieval") and not traj.retrievals():
        failures.append("retrieval never ran")

    expected_doc = requires.get("expect_doc_id")
    if expected_doc and expected_doc not in retrieved:
        failures.append(
            f"expected doc '{expected_doc}' not retrieved (got: {retrieved or 'nothing'})"
        )

    if requires.get("expect_empty"):
        # Deliberately not just "no docs" — the agent must have LOOKED and found
        # nothing. Skipping retrieval entirely also yields zero docs and is a
        # different, worse behaviour.
        if not traj.retrievals():
            failures.append("expected an empty retrieval, but retrieval never ran at all")
        elif not traj.retrieval_was_empty():
            failures.append(
                f"expected retrieval to find nothing, but it returned: {retrieved}"
            )

    if requires.get("harness_reject") and not traj.rejections():
        failures.append("expected the harness to block a proposed call; no rejection recorded")

    if requires.get("memory"):
        mem = [s for s in traj if s.kind == "memory"]
        if not mem:
            failures.append("prior-ticket history was never consulted")
        elif all(s.detail.get("n_prior_tickets", 0) == 0 for s in mem):
            failures.append("history was consulted but returned nothing for this customer")

    return failures


def run_fixture(fixture: dict, retriever: PolicyRetriever, provider, provider_name: str) -> TicketResult:
    """Run one fixture through a FRESH session.

    Fresh because conversation state would otherwise leak between fixtures and
    make results order-dependent — the same input could pass or fail depending
    on what ran before it, which would quietly destroy reproducibility.
    """
    ticket = Ticket(
        ticket_id=f"eval_{fixture['id']}",
        customer_id=fixture["customer_id"],
        ticket_type=fixture["ticket_type"],
    )
    session = Session(
        ticket=ticket,
        provider=provider,
        provider_name=provider_name,
        retriever=retriever,
    )

    started = time.time()
    try:
        answer = session.answer_turn(fixture["message"])
    except Exception as exc:  # noqa: BLE001 — a crash is a failure, not a stop
        return TicketResult(
            id=fixture["id"],
            passed=False,
            failures=[f"agent raised {type(exc).__name__}: {exc}"],
            trajectory=session.last_trajectory,
            elapsed=time.time() - started,
        )

    traj = session.last_trajectory or Trajectory()
    failures = score(fixture, traj)
    return TicketResult(
        id=fixture["id"],
        passed=not failures,
        failures=failures,
        trajectory=traj,
        answer=answer,
        elapsed=time.time() - started,
    )


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def build_run_payload(results: list[TicketResult]) -> dict:
    """The full, detailed result shape shared by --json and the baseline writer."""
    n_passed = sum(r.passed for r in results)
    return {
        "pass_rate": n_passed / len(results) if results else 0.0,
        "n_total": len(results),
        "n_passed": n_passed,
        "git_sha": git_sha(),
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "results": [
            {
                "id": r.id,
                "passed": r.passed,
                "failures": r.failures,
                "trajectory": r.trajectory.to_list() if r.trajectory else [],
                "answer": r.answer,
            }
            for r in results
        ],
    }


BASELINE_NOTE = (
    "Threshold is 5.0pp on a 12-fixture suite: one ticket is worth 8.3pp, so 5.0 "
    "sits below the value of a single ticket and any single-ticket regression trips "
    "the gate (PLAN-assignment-2.md §5.3). The Anthropic model (claude-sonnet-5) "
    "rejects a `temperature` parameter, so trajectory determinism was not pinned but "
    "verified empirically: the clean suite returned 12/12 with the same fixtures "
    "passing across five consecutive runs. The 5.0pp margin also absorbs any residual "
    "run-to-run LLM variance while still catching a real single-ticket regression."
)


def write_baseline(
    results: list[TicketResult],
    threshold_pp: float,
    path: str = BASELINE_PATH,
) -> dict:
    """Freeze the current clean run as the committed baseline (PLAN §6.3).

    Writes the compact shape the gate reads: the rate, the per-ticket pass map
    (so the before/after report can name which fixtures flipped), the SHA it was
    captured at, the threshold, and the reasoning behind that threshold.
    """
    n_passed = sum(r.passed for r in results)
    baseline = {
        "pass_rate": n_passed / len(results) if results else 0.0,
        "n_total": len(results),
        "n_passed": n_passed,
        "per_ticket": {r.id: r.passed for r in results},
        "git_sha": git_sha(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "threshold_pp": threshold_pp,
        "note": BASELINE_NOTE,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2)
    return baseline


def load_baseline(path: str = BASELINE_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compare_to_baseline(results: list[TicketResult], baseline: dict) -> bool:
    """Print the current-vs-baseline comparison and return True if it REGRESSED.

    'Regressed' means the pass rate dropped more than threshold_pp below the
    committed baseline. The predicate lives here in Python, not in the workflow
    YAML (PLAN §7) — main() turns the returned bool into the process exit code.
    """
    n_passed = sum(r.passed for r in results)
    current_rate = n_passed / len(results) if results else 0.0
    baseline_rate = baseline["pass_rate"]
    threshold_pp = baseline.get("threshold_pp", DEFAULT_THRESHOLD_PP)
    drop_pp = (baseline_rate - current_rate) * 100

    print("\n" + "=" * 72)
    print("BASELINE COMPARISON")
    print("-" * 72)
    print(f"  baseline pass rate : {baseline_rate:.1%}  ({baseline['n_passed']}/{baseline['n_total']}, sha {baseline['git_sha'][:8]})")
    print(f"  current  pass rate : {current_rate:.1%}  ({n_passed}/{len(results)})")
    print(f"  drop               : {drop_pp:+.1f}pp   (threshold {threshold_pp:.1f}pp)")

    # Name exactly which fixtures changed status, in both directions.
    prior = baseline.get("per_ticket", {})
    current = {r.id: r.passed for r in results}
    regressed = [fid for fid, ok in current.items() if prior.get(fid) and not ok]
    recovered = [fid for fid, ok in current.items() if fid in prior and not prior[fid] and ok]
    new_ids = [fid for fid in current if fid not in prior]
    dropped_ids = [fid for fid in prior if fid not in current]

    if regressed:
        print(f"  now failing        : {', '.join(regressed)}")
    if recovered:
        print(f"  now passing        : {', '.join(recovered)}")
    if new_ids:
        print(f"  new fixtures        : {', '.join(new_ids)} (not in baseline)")
    if dropped_ids:
        print(f"  missing fixtures    : {', '.join(dropped_ids)} (in baseline, not run)")

    print("-" * 72)
    is_regression = drop_pp > threshold_pp
    if is_regression:
        print(f"  RESULT: REGRESSION — drop {drop_pp:.1f}pp exceeds {threshold_pp:.1f}pp threshold")
    else:
        print(f"  RESULT: OK — drop {drop_pp:.1f}pp within {threshold_pp:.1f}pp threshold")
    print("=" * 72)
    return is_regression


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the trajectory eval suite.")
    parser.add_argument("--only", help="Run a single fixture by id.")
    parser.add_argument("--json", dest="json_out", help="Write full results to this path.")
    parser.add_argument("--quiet", action="store_true", help="Suppress agent stdout noise.")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Freeze this run as eval/baseline.json (run on the clean agent).",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Gate mode: compare against eval/baseline.json and exit 1 on a regression.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD_PP,
        help=f"Regression threshold in pp, only used with --write-baseline (default {DEFAULT_THRESHOLD_PP}).",
    )
    args = parser.parse_args()

    if args.write_baseline and args.baseline:
        print("--write-baseline and --baseline are mutually exclusive.")
        return 2
    if args.only and (args.write_baseline or args.baseline):
        # A baseline or a gate decision over a single fixture is meaningless.
        print("--only cannot be combined with --write-baseline or --baseline.")
        return 2

    fixtures = load_fixtures()
    if args.only:
        fixtures = [f for f in fixtures if f["id"] == args.only]
        if not fixtures:
            print(f"No fixture with id '{args.only}'.")
            return 2

    provider_name = os.getenv("LLM_PROVIDER", "anthropic")
    provider = get_provider(provider_name)
    # One retriever for the whole suite: the embedding model loads once instead
    # of twelve times, which is most of the runtime.
    retriever = PolicyRetriever()

    print(f"Running {len(fixtures)} fixtures  |  provider={provider_name}")
    print("=" * 72)

    results: list[TicketResult] = []
    for i, fixture in enumerate(fixtures, 1):
        print(f"\n[{i}/{len(fixtures)}] {fixture['id']} ({fixture['ticket_type']})")
        print(f'    "{fixture["message"]}"')
        result = run_fixture(fixture, retriever, provider, provider_name)
        results.append(result)
        mark = "PASS" if result.passed else "FAIL"
        print(f"    -> {mark}  ({result.elapsed:.1f}s)")
        if not result.passed:
            for reason in result.failures:
                print(f"       ! {reason}")
            print("       trajectory:")
            print(result.trajectory.summary() if result.trajectory else "         (none)")

    n_passed = sum(r.passed for r in results)
    pass_rate = n_passed / len(results) if results else 0.0

    print("\n" + "=" * 72)
    print(f"{'FIXTURE':<32} {'RESULT':<8} TIME")
    print("-" * 72)
    for r in results:
        print(f"{r.id:<32} {'PASS' if r.passed else 'FAIL':<8} {r.elapsed:>5.1f}s")
    print("-" * 72)
    print(f"pass rate: {n_passed}/{len(results)} = {pass_rate:.1%}")

    if args.json_out:
        payload = build_run_payload(results)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nwrote {args.json_out}")

    # --write-baseline: freeze this clean run for future comparisons (PLAN §6.3).
    if args.write_baseline:
        baseline = write_baseline(results, args.threshold)
        print(
            f"\nwrote {BASELINE_PATH}\n"
            f"  baseline: {baseline['n_passed']}/{baseline['n_total']} = {baseline['pass_rate']:.1%}"
            f"  |  threshold {baseline['threshold_pp']:.1f}pp  |  sha {baseline['git_sha'][:8]}"
        )
        return 0

    # --baseline: this is the gate. Compare, print, and let the regression bool
    # BE the exit code — the pass/fail decision is made here in Python, and the
    # workflow only reacts to the exit status (PLAN §7).
    if args.baseline:
        try:
            baseline = load_baseline()
        except FileNotFoundError:
            print(
                f"\nNo baseline at {BASELINE_PATH}. "
                f"Run `--write-baseline` on the clean agent first."
            )
            return 2
        regressed = compare_to_baseline(results, baseline)
        return 1 if regressed else 0

    # Stage 3 default: report only, always exit 0. The baseline comparison and
    # the non-zero exit that make this a gate live behind --baseline above.
    return 0


if __name__ == "__main__":
    sys.exit(main())
