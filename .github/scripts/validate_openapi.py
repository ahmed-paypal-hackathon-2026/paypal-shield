#!/usr/bin/env python3
"""Validate that shared/openapi.json is well-formed and covers the contract endpoints."""

import json
import sys
from pathlib import Path

REQUIRED_PATHS = {
    "/api/v1/quotes": {"post"},
    "/api/v1/orders": {"post"},
    "/api/v1/orders/{id}/capture": {"post"},
    "/api/v1/claims": {"post"},
    "/api/v1/claims/{id}": {"get"},
    "/api/v1/claims/{id}/resolve": {"post"},
    "/api/v1/ledger/transactions": {"get"},
    "/api/v1/ledger/merchants": {"get"},
    "/api/v1/ledger/summary": {"get"},
    "/api/v1/stream": {"get"},
    "/api/v1/webhooks/paypal": {"post"},
    "/api/v1/health": {"get"},
}


def main() -> int:
    path = Path(__file__).resolve().parents[2] / "shared" / "openapi.json"
    try:
        contract = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"FATAL: frozen contract missing at {path}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"FATAL: shared/openapi.json is not valid JSON: {exc}")
        return 1

    print(f"contract version: {contract['info']['version']}")
    errors = []
    for required, methods in REQUIRED_PATHS.items():
        item = contract.get("paths", {}).get(required)
        if item is None:
            errors.append(f"missing path {required}")
            continue
        for method in methods:
            if method not in item:
                errors.append(f"missing method {method.upper()} {required}")

    for schema in [
        "QuoteRequest",
        "QuoteResponse",
        "RiskAssessment",
        "ProtectionQuote",
        "CreateOrderRequest",
        "CreateOrderResponse",
        "CaptureResponse",
        "ClaimRecord",
        "LedgerSummary",
        "TransactionRow",
        "MerchantTrustRow",
    ]:
        if schema not in contract.get("components", {}).get("schemas", {}):
            errors.append(f"missing schema {schema}")

    for error in errors:
        print(f"CONTRACT ERROR: {error}")
    if errors:
        return 1
    print("openapi contract covers all endpoints and schemas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
