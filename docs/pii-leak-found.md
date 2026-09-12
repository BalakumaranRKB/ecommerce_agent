# Confident Wrong Path: The PII That Slipped Both Layers

**Phase 6 finding (spec §2.4 / SPEC-PII).**
**Status:** found, reproduced locally, and fixed. Toggle: `with_custom` in `pii.py`.

---

## The short version

The masking layer looked like it worked. Names, emails, and phone numbers came
out redacted, so a first glance said "PII is handled." But two India-specific
identifiers — **PAN** and **Aadhaar** — tell a different story. The PAN
(`AKPPK7821L`) sailed straight through Presidio's defaults **completely
un-redacted**. The Aadhaar (`3782 4629 1057`) *appeared* to be masked — but only
because spaCy's NER model **mislabelled the digits as a `DATE_TIME`**, a fluke of
the surrounding sentence, not a real catch. Change the sentence and that
accidental cover disappears. A defence that depends on a model guessing the wrong
label at the right moment is not a defence. The fix is a custom recognizer that
catches both **deterministically, by format**, regardless of context.

---

## Why this bug is even possible

Presidio's default recognizers are built for a largely US/EU entity set: person,
location, email, credit card, US SSN, phone, and so on. **Aadhaar and PAN are not
in that set** — there is no `IN_AADHAAR` or `IN_PAN` recognizer out of the box.
Bedrock Guardrails' Sensitive Information Filters have the same blind spot: they
cover NAME/EMAIL/PHONE-style entities, not Indian national IDs. So for an
India-facing store, the two most sensitive identifiers in the record are exactly
the two **neither default layer knows to look for**.

That is what makes the leak realistic rather than staged. `mock_data.py` seeds
real, format-valid Aadhaar and PAN values (`cust_1001`, `cust_4004`, `cust_2002`)
precisely so this finding is honest — the coverage matrix in `CUSTOMER_PII`'s
comment marks both as "Presidio default MISS / Bedrock MISS."

The subtle part is the Aadhaar. It's not that the digits always survive — it's
that whether they survive depends on **how spaCy happens to label them in that
particular sentence**. That non-determinism is worse than a clean miss: it makes
the gap look closed in a casual test and reopens it silently in production.

---

## The evidence (real output from `uv run python -m pii`)

Input reply for `cust_1001` (Ananya Krishnan), carrying name, +91 phone, Aadhaar,
and PAN:

```
Thanks Ananya Krishnan. We'll call +91 98765 43210 about the refund.
On file: Aadhaar 3782 4629 1057, PAN AKPPK7821L.
```

Broken (Presidio defaults only) vs fixed (defaults + custom Aadhaar/PAN
recognizers), same code, toggled by `with_custom`:

```
BROKEN:  Thanks <LOCATION>. We'll call <PHONE_NUMBER> about the refund.
         On file: <PERSON> <DATE_TIME>, <ORGANIZATION> AKPPK7821L.
FIXED:   Thanks <LOCATION>. We'll call <PHONE_NUMBER> about the refund.
         On file: <PERSON><IN_AADHAAR>, <ORGANIZATION> <IN_PAN>.
```

| | Broken (defaults only) | Fixed (+ custom recognizers) |
|---|---|---|
| PAN `AKPPK7821L` | ❌ **left fully intact** in the output | ✅ `<IN_PAN>` |
| Aadhaar `3782 4629 1057` | ⚠️ digits gone, but as `<DATE_TIME>` — a spaCy **misclassification**, not an Aadhaar catch; never typed `IN_AADHAAR` | ✅ `<IN_AADHAAR>` (deterministic, format-based) |
| Name / phone | masked (as `<PERSON>`/`<LOCATION>`, `<PHONE_NUMBER>`) | masked |

**The headline leak is the PAN:** un-redacted under the defaults, cleanly
`<IN_PAN>` after the fix. The Aadhaar is the subtler, more instructive half — the
defaults never recognized it *as* an Aadhaar at all; any masking there was
accidental and text-dependent.

> Note the spaced-vs-unspaced sub-case: the Aadhaar is seeded spaced
> (`3782 4629 1057`) as printed on a real card, and a naive `\d{12}` would miss
> it. The custom recognizer uses a separator-tolerant pattern
> (`\d{4}\s?\d{4}\s?\d{4}`), so spaced and unspaced both catch — asserted by
> `test_aadhaar_spacing_evasion`.

---

## The fix

In `pii.py`, two custom Presidio `PatternRecognizer`s are registered into the
analyzer — the recognizers Presidio's defaults never shipped:

```python
_AADHAAR = Pattern(name="aadhaar", regex=r"\b\d{4}\s?\d{4}\s?\d{4}\b", score=0.85)
_PAN     = Pattern(name="pan",     regex=r"\b[A-Z]{5}[0-9]{4}[A-Z]\b",  score=0.9)

analyzer.registry.add_recognizer(
    PatternRecognizer(supported_entity="IN_AADHAAR", patterns=[_AADHAAR],
                      context=["aadhaar", "uid", "uidai"]))
analyzer.registry.add_recognizer(
    PatternRecognizer(supported_entity="IN_PAN", patterns=[_PAN],
                      context=["pan", "permanent account"]))
```

The broken path is kept behind the `with_custom` toggle (`build_analyzer(with_custom=False)`)
rather than deleted, so both states run against the *same* code — the same idiom
as Phase 1's `DROP_STANDING_IN_HANDOFF` and Phase 5's `correlate_by`.

---

## How to reproduce it

```bash
uv run python -m pii              # broken-vs-fixed demo on the real seeded PII
uv run python test_pii_masking.py # 9/9: PAN leaks under defaults, IN_PAN/IN_AADHAAR after fix
```

`test_pan_leaks_under_defaults` asserts the PAN survives Presidio's defaults;
`test_pan_caught_by_custom_recognizer` and
`test_aadhaar_caught_deterministically_by_custom_recognizer` assert the fix;
`test_aadhaar_not_recognized_as_aadhaar_under_defaults` pins down that the
defaults never typed the Aadhaar as an Aadhaar (the fluke, not a catch).

---

## The layering win (Defensible #4)

The two layers are not redundant — each covers a gap the other leaves, and both
gaps are demonstrated on real strings, not asserted:

- **Presidio-with-custom catches what Bedrock misses: Aadhaar and PAN.** These are
  Indian national IDs outside Bedrock's Sensitive Information Filter set, and we
  deliberately did *not* add Aadhaar/PAN regexes to the guardrail (see
  `provision_guardrail.py`). So the managed layer alone leaks them; the custom
  recognizer is the only thing that catches them.
- **Bedrock catches what Presidio mangles: full postal addresses.** On
  `Ship to 14 2nd Cross, Bengaluru 560038`, Presidio's small spaCy model
  (`en_core_web_sm`) produced `Ship to <DATE_TIME> Cross, <LOCATION> <DATE_TIME>`
  — the house number and PIN mislabelled as dates and the street name **leaked**
  as the literal word "Cross." The Bedrock guardrail masked the entire span as one
  unit: `Ship to {ADDRESS}.` Verified directly against the live guardrail
  (`check_guardrail.py`): `action=GUARDRAIL_INTERVENED`, the ADDRESS entity
  `detected=true, action=ANONYMIZED`.

Raw evidence from `ApplyGuardrail` on the live guardrail (`dqyydvde07yq`):

```
ORIGINAL : ... Ship to 14 2nd Cross, Bengaluru 560038.
action   : GUARDRAIL_INTERVENED  ("Guardrail masked.")
output   : ... Ship to {ADDRESS}.
detected : NAME, PHONE, EMAIL, ADDRESS  (all ANONYMIZED)
```

**Why the reply string showed no Layer-2 difference:** on the customer-reply
string, Presidio already covered every entity, so Bedrock had nothing left to add
— the layers only visibly diverge on address-heavy text, where Presidio's `sm`
model is weak. That is the honest shape of the win: not "Bedrock always adds
something," but "each layer is the *only* one that catches a specific real class —
Aadhaar/PAN for Presidio-custom, clean addresses for Bedrock." Neither layer alone
is sufficient; the value is in the union.

> Note: the spec's illustrative guess was that Bedrock would catch the +91 phone
> Presidio softens. On this Presidio config that did **not** hold — Presidio caught
> the +91 phone cleanly. The real, tested difference is the address, above. This
> is exactly why the layering claim was verified against the live guardrail rather
> than copied from the spec.

---

## The lesson

A masking layer that redacts the *easy* entities can look complete while leaving
the *domain-specific* ones wide open — and worse, an NER model can make a gap
look closed by mislabelling PII as something harmless, so the leak hides until the
surrounding text changes. Coverage has to be verified **per entity type and by
the reason it was caught**, not by eyeballing whether a given string disappeared.
The mitigation is deterministic, format-based recognizers for the identifiers
your domain actually handles, layered under the managed filter — and a test that
asserts on the entity *type*, not just on whether the digits survived.
