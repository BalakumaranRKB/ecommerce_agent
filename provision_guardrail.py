"""
Phase 6 — Bedrock Guardrail provisioning (Layer 2 of the PII masker).

Run ONCE from an AWS-authenticated shell. Creates (or reuses) a Bedrock Guardrail
whose Sensitive Information Filters ANONYMIZE the built-in PII entities, then
prints the guardrail id + version you paste into the environment:

    uv run python provision_guardrail.py                 # create/reuse, print id+version
    uv run python provision_guardrail.py --region ap-south-1

Then, to turn Layer 2 on for the masker (pii.mask) and the live demo:

    export PII_GUARDRAIL_ID=<printed id>          # PowerShell: $env:PII_GUARDRAIL_ID="<id>"
    export PII_GUARDRAIL_VERSION=DRAFT

DESIGN NOTE — deliberately NOT Aadhaar/PAN
------------------------------------------
This guardrail masks only Bedrock's BUILT-IN entities (NAME, EMAIL, PHONE,
ADDRESS). We do NOT add Aadhaar/PAN regexes here on purpose: the honest §2.4
layering story is that Bedrock's defaults MISS Indian national IDs, and the
Presidio custom recognizer (Layer 1) is what catches them. Adding Aadhaar/PAN to
the guardrail would erase that contrast. Layer 2 exists to (a) provide a managed
second line for the common entities and (b) demonstrate the Presidio-vs-Bedrock
difference (Defensible #4), not to duplicate Layer 1's custom recognizers.
"""

from __future__ import annotations

import argparse
import json
import time

GUARDRAIL_NAME = "ordercare-pii-guardrail"
DEFAULT_REGION = "ap-south-1"

# Built-in Bedrock PII entities we ANONYMIZE (mask). Not Aadhaar/PAN — see note.
_PII_ENTITIES = ("NAME", "EMAIL", "PHONE", "ADDRESS")


def _find_existing(client) -> dict | None:
    resp = client.list_guardrails(maxResults=100)
    for g in resp.get("guardrails", []):
        if g.get("name") == GUARDRAIL_NAME:
            return g
    return None


def provision_guardrail(region: str) -> dict:
    import boto3

    client = boto3.client("bedrock", region_name=region)

    existing = _find_existing(client)
    if existing:
        gid = existing.get("id") or existing.get("guardrailId")
        ver = existing.get("version", "DRAFT")
        print(f"[guardrail] reusing existing {GUARDRAIL_NAME!r}: {gid} (v{ver})")
        return {"guardrail_id": gid, "guardrail_version": ver}

    print(f"[guardrail] creating {GUARDRAIL_NAME!r} in {region} (ANONYMIZE: "
          f"{', '.join(_PII_ENTITIES)}) ...")
    resp = client.create_guardrail(
        name=GUARDRAIL_NAME,
        description="OrderCare Layer-2 PII masking (Phase 6 / spec §2.4).",
        sensitiveInformationPolicyConfig={
            "piiEntitiesConfig": [
                {"type": t, "action": "ANONYMIZE"} for t in _PII_ENTITIES
            ],
        },
        blockedInputMessaging="Input blocked by guardrail.",
        blockedOutputsMessaging="Output blocked by guardrail.",
    )
    gid = resp["guardrailId"]
    ver = resp.get("version", "DRAFT")
    print(f"[guardrail] created: {gid} (v{ver}); waiting for READY ...")

    # create is async — poll until the guardrail is usable by ApplyGuardrail.
    for _ in range(30):
        status = client.get_guardrail(
            guardrailIdentifier=gid, guardrailVersion=ver
        ).get("status")
        if status == "READY":
            print(f"[guardrail] READY: {gid}")
            break
        if status == "FAILED":
            raise RuntimeError(f"guardrail {gid} entered FAILED status")
        time.sleep(2)
    else:
        print(f"[guardrail] still not READY after ~60s — check the console; "
              f"ApplyGuardrail will 4xx until it is.")
    return {"guardrail_id": gid, "guardrail_version": ver}


def main() -> int:
    ap = argparse.ArgumentParser(description="Provision the Phase 6 Bedrock guardrail.")
    ap.add_argument("--region", default=DEFAULT_REGION)
    args = ap.parse_args()

    out = {"region": args.region}
    out.update(provision_guardrail(args.region))

    print("\n=== Phase 6 guardrail (record these) ===")
    print(json.dumps(out, indent=2))
    print("\nNext — turn Layer 2 on:")
    print(f"  export PII_GUARDRAIL_ID={out['guardrail_id']}")
    print(f"  export PII_GUARDRAIL_VERSION={out['guardrail_version']}")
    print("  (PowerShell: $env:PII_GUARDRAIL_ID=\"...\" ; $env:PII_GUARDRAIL_VERSION=\"...\")")
    print("  then: uv run python -m pii   # now runs BOTH layers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
