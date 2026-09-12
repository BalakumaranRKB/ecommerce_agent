"""
Phase 4 exit-gate test (spec §2.7) — the adversarial amount case.

Proves the core claim of "a decision is data, not a sentence": once a typed
RefundDecision is produced through the structured-output tool-use seam in
llm.py, its `amount` field is correct even when the model's own prose is
phrased in a way that would trip up a naive prose-parser.

No network calls, no API key needed: a FakeStructuredProvider stands in for
a real backend and returns a canned response in EXACTLY the shape a real
provider's forced tool-use call returns (ModelResponse with
stop_reason="tool_use" and one ToolCall carrying the typed fields). This
tests the PLUMBING -- Provider.create_structured() + the RefundDecision
model -- not model judgement. See phase_4_implementation_plan.md: "The
adversarial test is specifically about representation, not judgment."

    uv run python test_structured_decision.py
"""

from __future__ import annotations

import re
import sys

from decisions import RefundDecision
from llm import ModelResponse, Provider, StructuredOutputError, ToolCall

# ---------------------------------------------------------------------------
# The adversarial phrasing straight from the Phase 4 plan: a partial refund,
# phrased so the REJECTED amount appears in the same sentence as the awarded
# one. A naive prose-parser has no notion of "not the full X" being a
# negation -- both numbers look equally valid to grab.
# ---------------------------------------------------------------------------
ADVERSARIAL_PROSE = (
    "I'll refund the ₹4,999 shipping portion, not the full ₹8,990 you asked for."
)
INTENDED_AMOUNT = 4999   # what was actually decided/refunded
DECOY_AMOUNT = 8990      # the customer's original ask, mentioned in the same sentence


def naive_prose_parse_amount(prose: str) -> int:
    """A plausible naive implementation someone might reach for before typed
    output existed: grab every rupee figure and take the LAST one. This is
    exactly the failure mode Phase 4 removes -- a regex can't tell "the
    amount awarded" apart from "the amount mentioned while explaining what
    was NOT awarded"."""
    matches = re.findall(r"₹\s?([\d,]+)", prose)
    if not matches:
        raise ValueError("no rupee amount found in prose")
    return int(matches[-1].replace(",", ""))


class FakeStructuredProvider(Provider):
    """Stands in for a real backend's forced-tool-use response. Returns
    exactly what AnthropicProvider / GroqProvider / BedrockProvider.create()
    return for a forced tool call: stop_reason='tool_use' plus one ToolCall
    whose `.input` holds the decision fields the test wants to check."""

    def __init__(self, tool_input: dict, text: str = ADVERSARIAL_PROSE):
        self._tool_input = tool_input
        self._text = text

    def create(self, system, messages, tools=None, tool_choice=None):
        # A real provider is forced (via tool_choice) to call the requested
        # tool; the fake just echoes that name back onto the ToolCall, same
        # as a real forced call would.
        name = tool_choice or (tools[0]["name"] if tools else "unknown")
        return ModelResponse(
            stop_reason="tool_use",
            text=self._text,
            tool_calls=[ToolCall(id="tc_1", name=name, input=self._tool_input)],
        )

    def _render_structured_tool(self, tool_spec):
        # No real wire format to translate to -- just pass the generic spec
        # through so create() above can read tool_spec["name"] via tools[0].
        return tool_spec

    def append_assistant_turn(self, messages, response):
        raise NotImplementedError

    def append_tool_results(self, messages, response, results):
        raise NotImplementedError


def test_typed_amount_survives_adversarial_prose() -> bool:
    """THE exit-gate test: the typed field is correct where a prose-parser errs."""
    provider = FakeStructuredProvider(tool_input={
        "order_id": "ord_6002",
        "customer_id": "cust_2002",
        "amount": INTENDED_AMOUNT,
        "action": "approve",
        "requires_human": False,
        "reasoning": ADVERSARIAL_PROSE,
    })

    decision = provider.create_structured(
        system="You are a billing agent.",
        messages=[{"role": "user", "content": "Refund my laptop stand order."}],
        response_model=RefundDecision,
    )

    naive_guess = naive_prose_parse_amount(ADVERSARIAL_PROSE)

    ok = True
    ok &= isinstance(decision, RefundDecision)
    ok &= decision.amount == INTENDED_AMOUNT
    ok &= naive_guess == DECOY_AMOUNT       # confirms the naive parser DOES get it wrong
    ok &= decision.amount != naive_guess    # the contrast that is the whole point

    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] typed RefundDecision.amount={decision.amount} is correct; "
          f"naive prose-parser would have returned {naive_guess} (wrong)")
    return ok


def test_missing_amount_fails_loudly() -> bool:
    """A missing amount must raise at the boundary, not silently become a
    wrong refund. Directly tests: 'A missing or malformed amount fails
    loudly at the boundary instead of silently becoming a wrong refund.'"""
    provider = FakeStructuredProvider(tool_input={
        "order_id": "ord_6002",
        "customer_id": "cust_2002",
        # "amount" omitted entirely
        "action": "approve",
        "requires_human": False,
        "reasoning": "missing amount on purpose",
    })

    try:
        provider.create_structured(
            system="You are a billing agent.",
            messages=[{"role": "user", "content": "Refund my order."}],
            response_model=RefundDecision,
        )
        print("  [FAIL] missing amount did not raise -- it should fail loudly")
        return False
    except StructuredOutputError as e:
        print(f"  [PASS] missing amount raised StructuredOutputError ({e})")
        return True


def test_wrong_type_amount_fails_loudly() -> bool:
    """A non-numeric amount ('forty five hundred') must also fail
    construction rather than being silently coerced or passed through."""
    provider = FakeStructuredProvider(tool_input={
        "order_id": "ord_6002",
        "customer_id": "cust_2002",
        "amount": "forty five hundred",
        "action": "approve",
        "requires_human": False,
        "reasoning": "non-numeric amount on purpose",
    })

    try:
        provider.create_structured(
            system="You are a billing agent.",
            messages=[{"role": "user", "content": "Refund my order."}],
            response_model=RefundDecision,
        )
        print("  [FAIL] non-numeric amount did not raise -- it should fail loudly")
        return False
    except StructuredOutputError as e:
        print(f"  [PASS] non-numeric amount raised StructuredOutputError ({e})")
        return True


def test_wrong_tool_called_fails_loudly() -> bool:
    """If the model calls some other tool instead of the requested one, that
    must also raise rather than silently returning nothing / the wrong type."""
    provider = FakeStructuredProvider(tool_input={"unexpected": "shape"})

    # Override the echoed tool name so it does NOT match what create_structured
    # asked for -- simulating a model that ignored the forced tool_choice.
    def _create(system, messages, tools=None, tool_choice=None):
        return ModelResponse(
            stop_reason="tool_use",
            text="",
            tool_calls=[ToolCall(id="tc_1", name="some_other_tool", input={"unexpected": "shape"})],
        )
    provider.create = _create  # type: ignore[method-assign]

    try:
        provider.create_structured(
            system="You are a billing agent.",
            messages=[{"role": "user", "content": "Refund my order."}],
            response_model=RefundDecision,
        )
        print("  [FAIL] wrong tool call did not raise -- it should fail loudly")
        return False
    except StructuredOutputError as e:
        print(f"  [PASS] wrong tool call raised StructuredOutputError ({e})")
        return True


def test_dispute_result_maps_cleanly() -> bool:
    """Step 3's mapping: the specialist's typed DisputeResult becomes the
    main agent's typed RefundDecision, with no second LLM decision involved."""
    from billing_specialist.schemas import DisputeResult

    result = DisputeResult(
        order_id="ord_6002",
        customer_id="cust_2002",
        approved=False,
        requires_human_review=True,
        refund_amount=8990,
        reasoning="Customer has a flagged account.",
        action="escalate_hitl",
    )
    decision = RefundDecision.from_dispute_result(result)

    ok = (
        decision.order_id == result.order_id
        and decision.customer_id == result.customer_id
        and decision.amount == result.refund_amount
        and decision.action == result.action
        and decision.requires_human == result.requires_human_review
        and decision.reasoning == result.reasoning
    )
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] DisputeResult -> RefundDecision mapping preserves amount/action/hitl")
    return ok


def main() -> int:
    print("=== Phase 4 exit gate: typed RefundDecision vs. prose-parsing ===\n")
    results = [
        test_typed_amount_survives_adversarial_prose(),
        test_missing_amount_fails_loudly(),
        test_wrong_type_amount_fails_loudly(),
        test_wrong_tool_called_fails_loudly(),
        test_dispute_result_maps_cleanly(),
    ]
    ok = all(results)
    print("\n" + ("All Phase 4 structured-output tests passed." if ok
                  else "Phase 4 structured-output TESTS FAILED -- see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
