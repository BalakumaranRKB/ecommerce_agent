"""
LLM-as-judge — fact-checking rubric for the things trajectory rules can't check.

WHAT THIS SCORES (and what it does NOT)
The trajectory eval checks whether the agent took the right steps. This judge
checks whether the agent's *answer text* is actually supported by the reference
facts it was given. These are different failure modes:

  Trajectory eval catches:  wrong doc retrieved, tool never called, skipped check.
  Judge catches:            hallucinated number, fabricated claim, misquoted policy.

Neither replaces the other. Both catch real failures. But only the trajectory
eval is deterministic enough to gate a build (PLAN §5.2).

THE RUBRIC
Fact-checking, specific, numeric — not "rate this response's quality."

Given:
  1. The customer's question
  2. The REFERENCE FACTS (the actual policy doc text or order record —
     not what the agent said)
  3. The agent's response

Score 0–10 on: how much of the response is supported by the reference.
List every specific claim that is NOT supported.

NON-DETERMINISM
LLM judges give different scores on different runs for the same input.
The assignment's §6 pitfall is explicit: "trusting a single LLM-judge run as a
verdict" is flagged. So we run ≥3 times per fixture and report the spread.
The spread is a finding, not a bug.

DOES NOT GATE
This module is never called by regression_eval.py or the CI workflow.
It is a human-readable report, run on-demand.

    uv run python -m eval.judge
    uv run python -m eval.judge --runs 5
    uv run python -m eval.judge --only delivery_late_compensation
    uv run python -m eval.judge --json eval/results/judge_scores.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent import Session
from llm import get_provider
from retriever import PolicyRetriever, load_policy_docs
from ticket import Ticket

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES_PATH = os.path.join(_HERE, "fixtures.json")

# The five fixtures we judge — chosen for maximum fact-checking value.
# These are fixtures where the answer should cite specific policy details,
# giving the rubric something concrete to verify.
JUDGED_FIXTURE_IDS = [
    "delivery_late_compensation",   # must cite shipping-delay-compensation terms
    "delivery_damaged",             # must cite damaged-or-missing-items process
    "refund_return_window",         # must cite return-eligibility window
    "refund_opened_electronics",    # must cite refund-eligibility terms
    "account_flagged_appeal",       # must cite account-suspension-appeals process
]

# Rubric prompt — fact-checking, specific, numeric.
JUDGE_SYSTEM_PROMPT = """\
You are a fact-checking judge evaluating an e-commerce support agent's response.

You will be given:
1. QUESTION — the customer's original question
2. REFERENCE FACTS — the actual policy documents and/or order records the agent
   had access to. This is the ground truth.
3. AGENT RESPONSE — what the agent actually told the customer.

YOUR TASK: Score how much of the agent's response is factually supported by the
reference facts. This is NOT about tone, helpfulness, or quality — it is about
whether every specific claim the agent makes actually appears in the reference.

SCORING RUBRIC (0-10):
  10 — Every factual claim in the response appears in the reference. No fabrication.
   8 — Almost all claims supported; one minor detail is unstated but reasonable.
   6 — Most claims supported, but one or two specific assertions are not in the reference.
   4 — Some claims supported, but multiple assertions are fabricated or contradict the reference.
   2 — Few claims supported; the response makes up most of its factual content.
   0 — Nothing in the response is supported by the reference.

WHAT COUNTS AS AN UNSUPPORTED CLAIM:
- A specific number, date, percentage, or time period not stated in the reference
- A policy rule or process step not described in the reference
- A factual assertion about the customer's order not present in the reference
- Stating that no relevant policy exists when the reference contains one

WHAT DOES NOT COUNT:
- Generic politeness ("I'm sorry to hear that") — not a factual claim
- Offering to escalate — procedural, not factual
- Hedging language ("I believe", "it appears") — these soften, not fabricate

OUTPUT FORMAT — respond with ONLY valid JSON, no other text:
{
  "score": <integer 0-10>,
  "unsupported_claims": ["<specific claim 1>", "<specific claim 2>"],
  "reasoning": "<one sentence explaining the score>"
}
"""


@dataclass
class JudgeResult:
    """Per-fixture judge result across multiple runs."""
    id: str
    runs: list[int] = field(default_factory=list)
    score_mean: float = 0.0
    score_min: int = 10
    score_max: int = 0
    unsupported: list[str] = field(default_factory=list)

    def compute_stats(self) -> None:
        if self.runs:
            self.score_mean = sum(self.runs) / len(self.runs)
            self.score_min = min(self.runs)
            self.score_max = max(self.runs)


def load_fixtures(path: str = FIXTURES_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["fixtures"]


def build_reference_facts(fixture: dict, policy_docs: dict[str, str]) -> str:
    """Build the reference facts block the judge compares against.

    This is the GROUND TRUTH — the actual policy doc text and/or a summary
    of the order record. The judge compares the agent's response to this,
    not to what the agent claimed it retrieved.
    """
    parts: list[str] = []

    # Include the expected policy doc if the fixture specifies one
    expected_doc = fixture.get("requires", {}).get("expect_doc_id")
    if expected_doc and expected_doc in policy_docs:
        parts.append(f"[POLICY DOCUMENT: {expected_doc}]\n{policy_docs[expected_doc]}")

    # For honest-gap fixtures, state explicitly that no policy exists
    if fixture.get("requires", {}).get("expect_empty"):
        parts.append("[NO RELEVANT POLICY EXISTS for this question in the knowledge base]")

    # For fixtures that require tools, note what data the tool would return
    for tool in fixture.get("requires", {}).get("tools", []):
        if tool == "lookup_order":
            parts.append(
                f"[ORDER DATA] The agent has access to look up order details "
                f"for customer {fixture['customer_id']} via the {tool} tool."
            )
        elif tool == "check_account_status":
            parts.append(
                f"[ACCOUNT DATA] The agent has access to check account status "
                f"for customer {fixture['customer_id']} via the {tool} tool."
            )

    return "\n\n".join(parts) if parts else "[No reference facts available]"


def judge_once(
    provider,
    question: str,
    reference_facts: str,
    agent_response: str,
) -> tuple[int, list[str], str]:
    """Run the judge rubric once. Returns (score, unsupported_claims, reasoning)."""
    user_prompt = (
        f"QUESTION:\n{question}\n\n"
        f"REFERENCE FACTS:\n{reference_facts}\n\n"
        f"AGENT RESPONSE:\n{agent_response}"
    )

    resp = provider.create(
        system=JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[],
    )

    # Parse the JSON response
    text = (resp.text or "").strip()
    # Handle potential markdown code blocks
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3].strip()
        if text.startswith("json"):
            text = text[4:].strip()

    try:
        result = json.loads(text)
        score = int(result.get("score", 0))
        score = max(0, min(10, score))  # clamp to 0-10
        unsupported = result.get("unsupported_claims", [])
        reasoning = result.get("reasoning", "")
        return score, unsupported, reasoning
    except (json.JSONDecodeError, ValueError, TypeError):
        print(f"    !! judge returned unparseable response: {text[:200]}")
        return 0, [f"unparseable judge response: {text[:100]}"], "parse error"


def run_agent_for_fixture(
    fixture: dict,
    retriever: PolicyRetriever,
    provider,
    provider_name: str,
) -> str:
    """Run the agent on one fixture and return its answer text."""
    ticket = Ticket(
        ticket_id=f"judge_{fixture['id']}",
        customer_id=fixture["customer_id"],
        ticket_type=fixture["ticket_type"],
    )
    session = Session(
        ticket=ticket,
        provider=provider,
        provider_name=provider_name,
        retriever=retriever,
    )
    return session.answer_turn(fixture["message"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="LLM-as-judge: fact-checking rubric for agent responses."
    )
    parser.add_argument(
        "--runs", type=int, default=3,
        help="Number of judge runs per fixture (default: 3, minimum: 3)."
    )
    parser.add_argument(
        "--only", help="Judge a single fixture by id."
    )
    parser.add_argument(
        "--json", dest="json_out",
        help="Write full results to this path."
    )
    args = parser.parse_args()
    n_runs = max(3, args.runs)  # assignment requires ≥3

    # Load fixtures — either the selected subset or a single one
    all_fixtures = load_fixtures()
    if args.only:
        fixtures = [f for f in all_fixtures if f["id"] == args.only]
        if not fixtures:
            print(f"No fixture with id '{args.only}'.")
            return 2
    else:
        fixtures = [f for f in all_fixtures if f["id"] in JUDGED_FIXTURE_IDS]

    # Load policy docs as reference facts
    raw_docs = load_policy_docs()
    policy_docs = {d["doc_id"]: d["text"] for d in raw_docs}

    provider_name = os.getenv("LLM_PROVIDER", "anthropic")
    provider = get_provider(provider_name)
    retriever = PolicyRetriever()

    print(f"LLM-as-judge  |  {len(fixtures)} fixtures x {n_runs} runs  |  provider={provider_name}")
    print("=" * 74)

    results: list[JudgeResult] = []

    for i, fixture in enumerate(fixtures, 1):
        print(f"\n[{i}/{len(fixtures)}] {fixture['id']}")
        print(f'    question: "{fixture["message"]}"')

        # Step 1: run the agent to get its actual answer
        print("    running agent...", end=" ", flush=True)
        started = time.time()
        try:
            answer = run_agent_for_fixture(fixture, retriever, provider, provider_name)
        except Exception as exc:
            print(f"AGENT ERROR: {exc}")
            results.append(JudgeResult(id=fixture["id"]))
            continue
        elapsed = time.time() - started
        print(f"done ({elapsed:.1f}s)")
        # Show a preview of the answer
        preview = answer[:120].replace("\n", " ")
        print(f'    answer:   "{preview}{"..." if len(answer) > 120 else ""}"')

        # Step 2: build reference facts
        reference = build_reference_facts(fixture, policy_docs)

        # Step 3: run the judge N times
        judge_result = JudgeResult(id=fixture["id"])
        all_unsupported: list[str] = []

        for run_idx in range(n_runs):
            score, unsupported, reasoning = judge_once(
                provider, fixture["message"], reference, answer
            )
            judge_result.runs.append(score)
            all_unsupported.extend(unsupported)
            print(f"    run {run_idx + 1}/{n_runs}: score={score}/10"
                  f"  {'OK' if not unsupported else '!! ' + str(len(unsupported)) + ' unsupported'}")

        # Deduplicate unsupported claims (different runs may flag the same thing)
        seen: set[str] = set()
        for claim in all_unsupported:
            normalized = claim.strip().lower()
            if normalized not in seen:
                seen.add(normalized)
                judge_result.unsupported.append(claim)

        judge_result.compute_stats()
        results.append(judge_result)
        spread = judge_result.score_max - judge_result.score_min
        print(f"    -> mean={judge_result.score_mean:.1f}  "
              f"min={judge_result.score_min}  max={judge_result.score_max}  "
              f"spread={spread}")

    # Summary table
    print("\n" + "=" * 74)
    print(f"{'FIXTURE':<32} {'MEAN':>5} {'MIN':>4} {'MAX':>4} {'SPREAD':>7}")
    print("-" * 74)
    for r in results:
        spread = r.score_max - r.score_min
        print(f"{r.id:<32} {r.score_mean:>5.1f} {r.score_min:>4} {r.score_max:>4} {spread:>7}")
    print("-" * 74)

    overall_mean = sum(r.score_mean for r in results) / len(results) if results else 0
    max_spread = max((r.score_max - r.score_min) for r in results) if results else 0
    print(f"overall mean: {overall_mean:.1f}/10   max spread: {max_spread}")

    if max_spread >= 4:
        print("\n!! Wide spread (>=4 points) -- judge scores vary significantly between runs.")
        print("   This is why judge scores REPORT but never GATE (PLAN S5.2).")
    elif max_spread >= 2:
        print(f"\nModerate spread ({max_spread} points) -- some run-to-run variance.")
    else:
        print(f"\nTight spread ({max_spread} points) -- judge is fairly consistent here.")

    # Unsupported claims summary
    any_unsupported = any(r.unsupported for r in results)
    if any_unsupported:
        print("\nUnsupported claims flagged by the judge:")
        for r in results:
            if r.unsupported:
                print(f"  {r.id}:")
                for claim in r.unsupported[:5]:  # cap at 5 per fixture
                    print(f"    - {claim}")

    # JSON output
    if args.json_out:
        payload = {
            "n_runs": n_runs,
            "provider": provider_name,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "overall_mean": round(overall_mean, 2),
            "results": [
                {
                    "id": r.id,
                    "runs": r.runs,
                    "score_mean": round(r.score_mean, 2),
                    "score_min": r.score_min,
                    "score_max": r.score_max,
                    "unsupported": r.unsupported,
                }
                for r in results
            ],
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
