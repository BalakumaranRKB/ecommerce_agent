"""
Phase 6 / spec §2.4 — layered PII masking tests.

Locks in the two things that matter, on the REAL seeded data (no AWS, Layer 1
only — Layer 2/Bedrock stays opt-in behind PII_GUARDRAIL_ID):

  1. The §2.4 CWP, broken vs fixed: PAN leaks under Presidio defaults and is
     caught by the custom recognizer; Aadhaar is never recognized AS an Aadhaar
     by defaults (any masking there is a spaCy misclassification fluke) and is
     caught deterministically by the fix.
  2. Every-egress redaction: a customer reply, a tool-error string, and a log
     line each carrying name/email/phone/Aadhaar/PAN come out with none of those
     raw values surviving.

Run:  uv run python -m tests.test_pii_masking   (or: uv run python test_pii_masking.py)
"""

from __future__ import annotations

from mock_data import CUSTOMER_PII, REFUND_REQUESTS
from pii import _presidio, mask

P = CUSTOMER_PII["cust_1001"]          # Ananya Krishnan — has Aadhaar + PAN
AADHAAR = P["aadhaar"]                  # "3782 4629 1057"
PAN = P["pan"]                          # "AKPPK7821L"


def _reply() -> str:
    return (f"Thanks {P['name']}. We'll call {P['phone']} about the refund. "
            f"On file: Aadhaar {AADHAAR}, PAN {PAN}.")


# --- 1. The §2.4 CWP: broken vs fixed --------------------------------------
def test_pan_leaks_under_defaults():
    """The clean, undeniable leak: Presidio defaults leave the PAN fully intact."""
    assert PAN in _presidio(_reply(), with_custom=False)


def test_pan_caught_by_custom_recognizer():
    out = _presidio(_reply(), with_custom=True)
    assert PAN not in out and "<IN_PAN>" in out


def test_aadhaar_not_recognized_as_aadhaar_under_defaults():
    """Defaults never label it IN_AADHAAR — any masking is a spaCy fluke, not a
    real catch. The honest signal is the entity TYPE, not whether digits survived."""
    assert "<IN_AADHAAR>" not in _presidio(_reply(), with_custom=False)


def test_aadhaar_caught_deterministically_by_custom_recognizer():
    out = _presidio(_reply(), with_custom=True)
    assert AADHAAR not in out and "<IN_AADHAAR>" in out


def test_aadhaar_spacing_evasion():
    """Separator-tolerant: unspaced Aadhaar is caught too (format-preserving evasion)."""
    unspaced = AADHAAR.replace(" ", "")
    out = _presidio(f"UID on file: {unspaced}", with_custom=True)
    assert unspaced not in out and "<IN_AADHAAR>" in out


# --- 2. Every egress: nothing raw survives ---------------------------------
def _assert_all_masked(text: str):
    out = mask(text)   # the production seam (Layer 1; Layer 2 if a guardrail is set)
    for raw in (P["name"], P["email"], P["phone"], AADHAAR, PAN):
        assert raw not in out, f"leaked {raw!r} in: {out!r}"


def test_egress_customer_reply():
    _assert_all_masked(_reply())


def test_egress_tool_error_string():
    """The classic error-path leak: req_r003's reason embeds a phone number."""
    err = f"RefundError on {PAN}: {REFUND_REQUESTS['req_r003']['reason']}"
    out = mask(err)
    assert REFUND_REQUESTS["req_r003"]["reason"].split("+91")[1][:6] not in out
    assert PAN not in out


def test_egress_log_line():
    log = (f"[trace] customer={P['name']} email={P['email']} phone={P['phone']} "
           f"aadhaar={AADHAAR} pan={PAN}")
    _assert_all_masked(log)


def test_empty_input_is_safe():
    assert mask("") == ""
    assert mask(None) is None  # type: ignore[arg-type]


if __name__ == "__main__":
    import sys, traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
