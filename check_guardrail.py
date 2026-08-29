"""
Diagnostic: does the Bedrock guardrail actually do anything?

Calls ApplyGuardrail directly on a PII-bearing string and prints the RAW response
(action, outputs, assessments) so we can see what Layer 2 really did — instead of
inferring it from whether the masked text changed. Needs PII_GUARDRAIL_ID set.

    uv run python check_guardrail.py
"""
from __future__ import annotations

import json
import os

import boto3

gid = os.environ["PII_GUARDRAIL_ID"]
ver = os.environ.get("PII_GUARDRAIL_VERSION", "DRAFT")
region = os.environ.get("AWS_REGION", "ap-south-1")

text = ("Thanks Ananya Krishnan. Call me on +91 98765 43210 or email "
        "ananya.krishnan@example.in. Ship to 14 2nd Cross, Bengaluru 560038.")

rt = boto3.client("bedrock-runtime", region_name=region)
resp = rt.apply_guardrail(
    guardrailIdentifier=gid,
    guardrailVersion=ver,
    source="OUTPUT",
    content=[{"text": {"text": text}}],
)

print(f"guardrail={gid} v{ver} region={region}\n")
print("ORIGINAL :", text, "\n")
print("action        :", resp.get("action"))
print("actionReason  :", resp.get("actionReason"))
outs = resp.get("outputs") or []
print("outputs[0].text:", outs[0]["text"] if outs else "(none)")
print("\nassessments (what the guardrail detected):")
print(json.dumps(resp.get("assessments", []), indent=2, default=str))
