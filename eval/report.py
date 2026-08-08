"""
Before/after report — the specific number, and which tickets flipped.

PLAN-assignment-2.md Stage 8. The bar the plan sets is a phrasing bar: the
output must say "100.0% -> 66.7%, and these 4 tickets flipped", not "performs
worse". A rate on its own tells you something moved; naming the fixtures tells
you what to go and look at.

WHY TWO SUBPROCESSES AND NOT TWO GIT REFS
The plan's wording is "run the suite against two refs (clean and regressed)",
which assumes the regression is a code difference between two commits. Ours is
not: Stage 7 regresses via the AGENT_REGRESSED environment variable, so the
clean and regressed commits are behaviourally IDENTICAL outside CI and checking
one out would measure nothing. The equivalent here is two environments, not two
refs.

They have to be separate PROCESSES, not one process with the variable toggled:
tools_schema.py reads AGENT_REGRESSED at import time and filters TOOL_SPECS at
module level, so by the time this script could change os.environ the filtering
has already happened. Subprocesses also guarantee no state leaks between the
two runs, which is what makes the comparison trustworthy.

WHY THE CLEAN SIDE IS MEASURED FRESH
eval/baseline.json already holds a clean 12/12 and using it would halve the
runtime. It is measured live anyway, because the two halves of a number that
goes on screen should come from the same session: trajectories are known to vary
run to run (classification wobbles; memory_prior_resolution sometimes dispatches
a tool and sometimes does not) even though the verdicts have been stable. Pairing
a live regressed number against an hour-old clean one is fine right up until the
once it isn't.

    uv run python -m eval.report
    uv run python -m eval.report --from-json before.json after.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BEFORE = os.path.join(_HERE, "report_before.json")
DEFAULT_AFTER = os.path.join(_HERE, "report_after.json")

# The one environment difference between the two runs. Withholds lookup_order
# from the model (tools_schema.py) — see Stage 7.
REGRESSION_ENV = {"AGENT_REGRESSED": "true"}


def run_suite(label: str, json_path: str, extra_env: dict | None = None) -> dict:
    """Run the full eval suite in a subprocess and return its --json payload.

    Output is streamed rather than captured: this takes minutes, and a silent
    terminal is indistinguishable from a hung one.
    """
    env = os.environ.copy()
    env.update(extra_env or {})
    # Make sure the clean run is genuinely clean even if the caller's shell
    # already has the flag exported.
    if not extra_env:
        env.pop("AGENT_REGRESSED", None)

    print("\n" + "#" * 72)
    print(f"# RUNNING: {label}")
    print("#" * 72)

    proc = subprocess.run(
        [sys.executable, "-m", "eval.regression_eval", "--json", json_path],
        env=env,
        cwd=os.path.dirname(_HERE),
    )
    # Note: a non-zero exit is NOT fatal here. The bare eval always exits 0, but
    # even if it did not, this script reports on results rather than gating on
    # them — the gate is regression_eval --baseline.
    if proc.returncode not in (0,):
        print(f"  (suite exited {proc.returncode}; continuing — this script reports, it does not gate)")

    return load_run(json_path)


def load_run(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def per_ticket(run: dict) -> dict[str, bool]:
    return {r["id"]: r["passed"] for r in run["results"]}


def failure_reasons(run: dict) -> dict[str, list[str]]:
    return {r["id"]: r.get("failures", []) for r in run["results"]}


def print_report(before: dict, after: dict, before_label: str, after_label: str) -> None:
    b_map, a_map = per_ticket(before), per_ticket(after)
    a_reasons = failure_reasons(after)

    b_rate, a_rate = before["pass_rate"], after["pass_rate"]
    delta_pp = (a_rate - b_rate) * 100

    print("\n" + "=" * 72)
    print("BEFORE / AFTER — trajectory pass rate")
    print("=" * 72)
    print(f"  BEFORE  {before_label:<26} : {before['n_passed']:>2}/{before['n_total']} = {b_rate:>6.1%}")
    print(f"  AFTER   {after_label:<26} : {after['n_passed']:>2}/{after['n_total']} = {a_rate:>6.1%}")
    print(f"  DELTA   {'':<26} : {delta_pp:>+6.1f}pp")
    print("-" * 72)

    # Per-fixture status, in the suite's own order, with flips called out.
    all_ids = list(b_map) + [i for i in a_map if i not in b_map]
    print(f"{'FIXTURE':<32} {'BEFORE':<8} {'AFTER':<8}")
    print("-" * 72)
    for fid in all_ids:
        b = b_map.get(fid)
        a = a_map.get(fid)
        b_txt = "PASS" if b else ("FAIL" if b is not None else "--")
        a_txt = "PASS" if a else ("FAIL" if a is not None else "--")
        flag = ""
        if b is not None and a is not None and b != a:
            flag = "  <- FLIPPED TO FAIL" if b and not a else "  <- FLIPPED TO PASS"
        print(f"{fid:<32} {b_txt:<8} {a_txt:<8}{flag}")
    print("-" * 72)

    # The headline sentence — a number and a named list, per Stage 8.
    broke = [i for i in all_ids if b_map.get(i) and a_map.get(i) is False]
    fixed = [i for i in all_ids if b_map.get(i) is False and a_map.get(i)]

    if not broke and not fixed:
        print(f"  {b_rate:.1%} -> {a_rate:.1%}: no tickets flipped.")
    else:
        if broke:
            print(f"  {b_rate:.1%} -> {a_rate:.1%}, and {len(broke)} of {before['n_total']} tickets flipped to FAIL:")
            for fid in broke:
                print(f"    - {fid}")
                # Why it broke, not just that it broke.
                for reason in a_reasons.get(fid, []):
                    print(f"        ! {reason}")
        if fixed:
            print(f"  {len(fixed)} ticket(s) flipped to PASS: {', '.join(fixed)}")

    print("=" * 72)
    print(f"  before sha: {before.get('git_sha', 'unknown')[:8]}   ran: {before.get('ran_at', '?')}")
    print(f"  after  sha: {after.get('git_sha', 'unknown')[:8]}   ran: {after.get('ran_at', '?')}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Before/after trajectory eval report (clean vs regressed)."
    )
    parser.add_argument(
        "--from-json",
        nargs=2,
        metavar=("BEFORE", "AFTER"),
        help="Skip both runs and render the report from two saved --json files.",
    )
    parser.add_argument("--before-json", default=DEFAULT_BEFORE, help="Where to write the clean run.")
    parser.add_argument("--after-json", default=DEFAULT_AFTER, help="Where to write the regressed run.")
    args = parser.parse_args()

    # --from-json exists because getting the table formatting right takes a few
    # attempts and each real invocation is two full suites of live API calls.
    if args.from_json:
        try:
            before, after = load_run(args.from_json[0]), load_run(args.from_json[1])
        except FileNotFoundError as exc:
            print(f"Could not read saved run: {exc}")
            return 2
        print_report(before, after, "clean agent", "AGENT_REGRESSED=true")
        return 0

    before = run_suite("BEFORE — clean agent", args.before_json)
    after = run_suite("AFTER — AGENT_REGRESSED=true", args.after_json, REGRESSION_ENV)

    print_report(before, after, "clean agent", "AGENT_REGRESSED=true")
    print(f"\nsaved: {args.before_json}\n       {args.after_json}")
    print("re-render without re-running:")
    print(f"  uv run python -m eval.report --from-json {args.before_json} {args.after_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
