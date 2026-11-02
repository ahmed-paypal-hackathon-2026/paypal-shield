"""REQ-OPS-02: the frozen shared/openapi.json contract stays canonical.

Runs without importing Person 1's FastAPI app: validates the frozen file's
shape covers every REST endpoint of spec section 5.4. The CI contract job
additionally diffs the *generated* spec against this file once P1 lands.
"""

import json
from pathlib import Path

import pytest

CONTRACT = Path(__file__).resolve().parents[3] / "shared" / "openapi.json"


def test_frozen_contract_exists_and_parses():
    data = json.loads(CONTRACT.read_text())
    assert data["openapi"].startswith("3.1")
    assert data["info"]["version"] == "1.0.0"


def test_all_spec_endpoints_are_declared():
    data = json.loads(CONTRACT.read_text())
    paths = data["paths"]
    expected = [
        "POST /api/v1/quotes",
        "POST /api/v1/orders",
        "POST /api/v1/orders/{id}/capture",
        "POST /api/v1/claims",
        "GET /api/v1/claims/{id}",
        "POST /api/v1/claims/{id}/resolve",
        "GET /api/v1/ledger/transactions",
        "GET /api/v1/ledger/merchants",
        "GET /api/v1/ledger/summary",
        "GET /api/v1/stream",
        "POST /api/v1/webhooks/paypal",
        "GET /api/v1/health",
    ]
    for entry in expected:
        method, path = entry.split(" ")
        assert method.lower() in paths[path], f"{entry} missing from frozen contract"


@pytest.mark.parametrize(
    "schema",
    [
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
    ],
)
def test_canonical_schemas_are_declared(schema: str):
    data = json.loads(CONTRACT.read_text())
    assert schema in data["components"]["schemas"]


def test_money_fields_are_strings_not_numbers():
    """The frontend never does float money math; the API must keep string money."""
    data = json.loads(CONTRACT.read_text())
    schemas = data["components"]["schemas"]
    for name, numeric_fields in [
        ("ProtectionQuote", ["item_price", "protection_fee"]),
        ("CreateOrderResponse", ["total"]),
        ("TransactionRow", ["item_price", "protection_fee"]),
        ("LedgerSummary", ["total_volume", "protection_fees_collected", "refunds_issued"]),
    ]:
        for field in numeric_fields:
            assert schemas[name]["properties"][field]["type"] == "string", f"{name}.{field}"
