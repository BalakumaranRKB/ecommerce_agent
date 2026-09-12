"""
Layered PII masking (Phase 6 / spec §2.4, SPEC-PII).

WHAT THIS IS
------------
One masking seam every egress calls: `mask(text)`. It runs TWO layers that stack:

  Layer 1  Presidio (in our own code) — Analyzer detects PII, Anonymizer redacts.
  Layer 2  Bedrock Guardrails (managed) — ApplyGuardrail, opt-in via env var.

Strong by itself, Presidio's DEFAULTS still miss India-specific IDs — Aadhaar and
PAN — and Bedrock's built-ins miss them too. That gap is the §2.4 "confident wrong
path": a first-pass masker leaks cust_1001's Aadhaar (3782 4629 1057) and PAN
(AKPPK7821L) straight into a reply or a log. The fix is a CUSTOM recognizer —
built below and toggled by `with_custom`, so the broken (defaults only) and fixed
(defaults + custom) states run against the SAME code, side by side, exactly like
the Phase 1 dropped-handoff and Phase 5 double-refund CWPs.

THE ONE LEVER: `with_custom`
----------------------------
`build_analyzer(with_custom=False)` reproduces the leak (Aadhaar/PAN survive).
`build_analyzer(with_custom=True)` (the default) is the fix. `mask()` uses the
fixed analyzer; the demo/tests drive both to show broken vs fixed.

LAYER 2 IS OPT-IN
-----------------
Layer 2 fires only when `PII_GUARDRAIL_ID` is set, so the local test suite runs
Layer-1-only with no AWS — same discipline as Phase 5's in-memory store default.
Set `PII_GUARDRAIL_ID` (+ optional `PII_GUARDRAIL_VERSION`) for the live demo.

REDACTION STRATEGY (§4.4)
-------------------------
Full replacement with an entity tag (`<IN_AADHAAR>`, `<PHONE_NUMBER>`, ...). Safest
(no residual digits), at the cost of readability. Partial (`+91 ····· 3210`) would
keep support usability but leak the last 4; tokenization would be reversible with a
vault — a feature and a liability. We choose full; documented in the README.

    uv run python -m pii        # broken-vs-fixed demo on the real seeded PII
"""

from __future__ import annotations

import os
from functools import lru_cache

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine

# --- the §2.4 fix: recognizers Presidio's defaults do not ship -------------
# Aadhaar: 12 digits, printed in 4-4-4 groups. Separator-tolerant so spaced
# (3782 4629 1057) and unspaced (378246291057) both match — the spec's
# "format-preserving evasion" sub-case.
_AADHAAR = Pattern(name="aadhaar", regex=r"\b\d{4}\s?\d{4}\s?\d{4}\b", score=0.85)
# PAN: five letters, four digits, one letter (AKPPK7821L).
_PAN = Pattern(name="pan", regex=r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", score=0.9)

_ANONYMIZER = AnonymizerEngine()

# Entities we redact at PRODUCTION egresses (mask()). Restricting to real PII
# keeps Presidio's noisy non-PII recognizers -- above all DATE_TIME firing on
# policy timeframes like "30 days" / "5 business days" -- from punching holes in
# legitimate reply text. PERSON and LOCATION both catch names (the small spaCy
# model sometimes labels a name LOCATION); LOCATION also covers addresses.
# ORGANIZATION/DATE_TIME are intentionally excluded (they fire on label words
# like "PAN" and on policy dates); Layer 2 (Bedrock NAME filter) is the backstop
# for any name the small model mislabels. The broken-vs-fixed CWP demo below
# calls _presidio() with entities=None (ALL entities) on purpose, so its finding
# is unchanged.
_MASK_ENTITIES = ["PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "LOCATION",
                  "IN_AADHAAR", "IN_PAN"]


@lru_cache(maxsize=2)
def build_analyzer(with_custom: bool = True) -> AnalyzerEngine:
    """Build the Presidio analyzer. `with_custom=False` reproduces the CWP (no
    Aadhaar/PAN recognizer); `with_custom=True` (default) is the fix. Cached so
    the spaCy model loads once per variant."""
    nlp = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        # Light model to keep the download small; upgrade to en_core_web_lg for
        # stronger NAME/LOCATION recall if the deadline allows.
        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
    }).create_engine()

    analyzer = AnalyzerEngine(nlp_engine=nlp)
    if with_custom:
        analyzer.registry.add_recognizer(PatternRecognizer(
            supported_entity="IN_AADHAAR", patterns=[_AADHAAR],
            context=["aadhaar", "uid", "uidai"]))
        analyzer.registry.add_recognizer(PatternRecognizer(
            supported_entity="IN_PAN", patterns=[_PAN],
            context=["pan", "permanent account"]))
    return analyzer


def _presidio(text: str, with_custom: bool = True,
              entities: list[str] | None = None) -> str:
    analyzer = build_analyzer(with_custom)
    # entities=None -> detect every default entity (used by the CWP demo);
    # entities=_MASK_ENTITIES -> production egress scope (no DATE_TIME noise).
    results = analyzer.analyze(text=text, language="en", entities=entities)
    return _ANONYMIZER.anonymize(text=text, analyzer_results=results).text


def _bedrock_guardrail(text: str, guardrail_id: str,
                       version: str | None = None,
                       region: str = "ap-south-1") -> str:
    """Layer 2. Lazy boto3 import so importing this module (and the tests) needs
    no AWS. Returns the guardrail-masked output when Sensitive Information Filters
    fire, else the input unchanged."""
    import boto3  # lazy: keeps tests offline
    rt = boto3.client("bedrock-runtime", region_name=region)
    resp = rt.apply_guardrail(
        guardrailIdentifier=guardrail_id,
        guardrailVersion=version or os.environ.get("PII_GUARDRAIL_VERSION", "DRAFT"),
        source="OUTPUT",
        content=[{"text": {"text": text}}],
    )
    outputs = resp.get("outputs") or []
    return outputs[0]["text"] if outputs and "text" in outputs[0] else text


def mask(text: str) -> str:
    """The single masking seam every egress calls. Layer 1 always; Layer 2 when a
    guardrail is configured. Never raises on empty/None-ish input."""
    if not text:
        return text
    out = _presidio(text, with_custom=True, entities=_MASK_ENTITIES)  # Layer 1
    gid = os.environ.get("PII_GUARDRAIL_ID")
    if gid:
        try:
            out = _bedrock_guardrail(out, gid)       # Layer 2 (opt-in)
        except Exception as e:  # never let a masking layer crash an egress
            print(f"[pii] Layer 2 (Bedrock) skipped: {e}")
    return out


# ---------------------------------------------------------------------------
# Broken-vs-fixed demo on the REAL seeded PII (no AWS, no spaCy download needed
# for the Aadhaar/PAN regex path, but the analyzer will load en_core_web_sm).
#   uv run python -m pii
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from mock_data import CUSTOMER_PII, REFUND_REQUESTS

    p = CUSTOMER_PII["cust_1001"]
    reply = (f"Thanks {p['name']}. We'll call {p['phone']} about the refund. "
             f"On file: Aadhaar {p['aadhaar']}, PAN {p['pan']}.")
    err = REFUND_REQUESTS["req_r003"]["reason"]  # embeds +91 90000 12345

    print("=== §2.4 PII masking: broken (defaults only) vs fixed (+ custom) ===\n")
    print("ORIGINAL reply:\n ", reply, "\n")
    print("BROKEN  (no Aadhaar/PAN recognizer) — the leak:\n ",
          _presidio(reply, with_custom=False), "\n")
    print("FIXED   (custom Aadhaar/PAN recognizers):\n ",
          _presidio(reply, with_custom=True), "\n")
    print("ERROR-PATH string (req_r003 reason), masked:\n ",
          _presidio(err, with_custom=True), "\n")

    # --- Layer comparison (Defensible #4): Layer 1 alone vs mask() = both layers.
    # Only meaningful when PII_GUARDRAIL_ID is set; otherwise both columns match.
    gid = os.environ.get("PII_GUARDRAIL_ID")
    print("=== Layer 1 (Presidio) alone  vs  mask() = Layer 1 + Layer 2 (Bedrock) ===")
    print(f"    (PII_GUARDRAIL_ID={'set: ' + gid if gid else 'UNSET -> Layer 2 off, columns will match'})\n")
    l1_only = _presidio(reply, with_custom=True)
    both = mask(reply)
    print("  Layer 1 only :\n   ", l1_only, "\n")
    print("  Both layers  :\n   ", both, "\n")
    if gid and l1_only != both:
        print("  -> Layer 2 CHANGED the output; the diff is your real Presidio-vs-Bedrock example.")
    elif gid:
        print("  -> Layer 2 ran but changed nothing here; Presidio already covered this string.")

    broken_out = _presidio(reply, with_custom=False)
    fixed_out = _presidio(reply, with_custom=True)

    # PAN is the clean, undeniable leak: defaults leave AKPPK7821L fully intact.
    pan_leaks_broken = "AKPPK7821L" in broken_out
    pan_fixed = "<IN_PAN>" in fixed_out and "AKPPK7821L" not in fixed_out
    # Aadhaar is subtler: in broken mode spaCy may mask the digits by MISLABELLING
    # them (DATE_TIME/PERSON), which is a fluke of the surrounding text, not a
    # real Aadhaar catch. The honest signal is the ENTITY TYPE, not whether the
    # digits survived: no <IN_AADHAAR> in broken, a deterministic <IN_AADHAAR> in fixed.
    aadhaar_not_typed_broken = "<IN_AADHAAR>" not in broken_out
    aadhaar_fixed = "<IN_AADHAAR>" in fixed_out

    print("  PAN     — " + ("LEAK reproduced" if pan_leaks_broken else "??")
          + ": defaults leave AKPPK7821L intact.")
    print("            " + ("FIX confirmed" if pan_fixed else "??")
          + ": custom recognizer -> <IN_PAN>.")
    print("  Aadhaar — " + ("confirmed" if aadhaar_not_typed_broken else "??")
          + ": defaults never recognize it AS an Aadhaar; any masking there is a")
    print("            spaCy misclassification (DATE_TIME), not a real catch.")
    print("            " + ("FIX confirmed" if aadhaar_fixed else "??")
          + ": custom recognizer -> <IN_AADHAAR> (deterministic, format-based).")
