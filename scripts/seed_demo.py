#!/usr/bin/env python3
"""Demo seeding script (REQ-OPS-05, Person 4).

Seeds the three canonical demo scenarios end to end against a running
PayPal Shield stack: quotes the risk engine, creates and captures orders,
files sample claims, and prints the expected verdicts so you can eyeball
that the closed loop (quote -> order -> capture -> claim -> refund) matches
the spec's verified worked examples.

Usage:
  uv run? no plain python:
  python scripts/seed_demo.py                       # seeds http://localhost:8000
  python scripts/seed_demo.py --base-url https://shield-api.onrender.com
  python scripts/seed_demo.py --dry-run             # prints the request plan, no network
  python scripts/seed_demo.py --scenarios trusted,scam

Wait for the seeded scam claim to walk to REFUNDED and for the next quote on
the same merchant to show m_penalty 1.50 -- that is the closed-loop reconciliation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "shared" / "fixtures" / "demo_scenarios.json"

DEFAULT_BASE = "http://localhost:8000"
CLAIM_PHOTO = (REPO / "shared" / "fixtures" / "images" / "valid.png").read_bytes()


def load_scenarios(keys: list[str] | None) -> list[dict]:
    doc = json.loads(FIXTURES.read_text())
    scenarios = doc["scenarios"]
    if keys:
        scenarios = [s for s in scenarios if s["key"] in keys]
    return scenarios


def plan(scenarios: list[dict]) -> list[dict]:
    steps: list[dict] = []
    for scenario in scenarios:
        key = scenario["key"]
        item = scenario["item"]
        steps.append(
            {
                "scenario": key,
                "type": "quote",
                "endpoint": "POST /api/v1/quotes",
                "request": {
                    "merchant_id": scenario["merchant"]["merchant_id"],
                    "item_hash": item.get("item_hash", ""),
                    "item_title": item["item_title"],
                    "item_price": item["item_price"],
                    "currency_code": item["currency_code"],
                },
                "expected": scenario["expected"],
            }
        )
    return steps


def call_json(base: str, path: str, payload: dict) -> dict | tuple[int, dict]:
    request = urllib.request.Request(  # nosec B310
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310
            body = response.read()
        return json.loads(body)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def upload_claim(base: str, order_id: str) -> dict | None:
    boundary = "shield-demo-photo"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="order_id"\r\n\r\n{order_id}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="received.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + CLAIM_PHOTO + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"{base}/api/v1/claims",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"  claim upload failed: HTTP {exc.code}")
        return None


def seed(base: str, scenarios: list[dict], opt_in: bool = True) -> bool:
    ok_all = True
    for entry in plan(scenarios):
        scenario_key = entry["scenario"]
        expected = entry["expected"]
        print(f"\n== {scenario_key} ==")
        print(f"  quote: {base}/api/v1/quotes merchant={entry['request']['merchant_id']}")
        response = call_json(base, "/api/v1/quotes", entry["request"])
        if isinstance(response, tuple):
            status, body = response
            print(f"  quote failed: HTTP {status} {body}")
            ok_all = False
            continue

        assessment = response["assessment"]
        r_final = str(assessment["r_final"])
        gate = assessment["gate"]
        matches = r_final == expected["r_final"] and gate == expected["gate"]
        fee = str(response["quote"]["protection_fee"]) if response.get("quote") else None
        fee_matches = fee == expected["protection_fee"]
        print(f"  r_final={r_final} (expected {expected['r_final']}) -> {gate}  {'OK' if matches else 'MISMATCH'}")
        print(f"  fee={fee} (expected {expected['protection_fee']})  {'OK' if fee_matches else 'MISMATCH'}")

        if not matches or not fee_matches:
            ok_all = False

        quote_id = (response.get("quote") or {}).get("quote_id", "")
        if not quote_id:
            print("  no quote available (blocked or unsupported); order + claim steps skipped")
            continue

        order = call_json(
            base,
            "/api/v1/orders",
            {"quote_id": quote_id, "opt_in": opt_in, "buyer_ref": f"seed-{scenario_key}"},
        )
        if isinstance(order, tuple):
            status, body = order
            print(f"  order failed: HTTP {status} {body}")
            ok_all = False
            continue
        capture = call_json(base, f"/api/v1/orders/{order['order_id']}/capture", {})
        if isinstance(capture, tuple):
            status, body = capture
            print(f"  capture failed: HTTP {status} {body}")
            ok_all = False
            continue
        print(f"  captured: order={order['internal_order_id']} capture={capture['paypal_capture_id']}")

        claim = upload_claim(base, order["internal_order_id"])
        if claim:
            print(f"  claim filed: {claim['claim_id']} state={claim['state']} (pipeline runs in background)")
        else:
            ok_all = False

    print("\nWait for SSE/wallet-webhook state changes; the scam claim reaches REFUNDED,")
    print("then the NEXT quote for the same merchant raises m_penalty to 1.50.")
    return ok_all


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the three PayPal Shield demo scenarios.")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="API base URL (default: local docker)")
    parser.add_argument("--scenarios", default="", help="comma separated: trusted,scam,offender")
    parser.add_argument("--opt-in-off", action="store_true", help="create orders without the Protection Plan")
    parser.add_argument("--dry-run", action="store_true", help="print the seeding plan without network calls")
    args = parser.parse_args()

    keys = [k.strip() for k in args.scenarios.split(",") if k.strip()] or None
    scenarios = load_scenarios(keys)
    steps = plan(scenarios)

    if args.dry_run:
        print(json.dumps(steps, indent=2))
        return 0

    print(f"seeding {len(scenarios)} scenario(s) against {args.base_url}")
    started = time.time()
    ok = seed(args.base_url, scenarios, opt_in=not args.opt_in_off)
    print(f"\nseed finished in {time.time() - started:.1f}s: {'ALL CASES MATCHED' if ok else 'MISMATCHES FOUND'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
