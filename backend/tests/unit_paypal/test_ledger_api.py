"""Ledger API tests (Person 1, frozen contract /api/v1/ledger/*).

All scenarios run through real demo-chain flows (quote -> order -> capture,
claim -> reviewer refund) against the real app — the ledger must reflect
persisted state only and never create or duplicate financial rows on read.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Settings

QUOTE_BODY = {
    "merchant_id": "mer_ledger_e2e",
    "item_hash": "c" * 64,
    "item_title": "Ledger Gadget",
    "item_price": "80.00",
    "currency_code": "USD",
}


def _client(tmp_path) -> TestClient:
    settings = Settings(
        _env_file=None,
        engine_impl="stub",
        demo_deterministic_mode=True,
        paypal_client_id="",
        paypal_client_secret="",
        sqlite_url=f"sqlite+aiosqlite:///{tmp_path}/ledger.db",
        quotes_hmac_key="ledger-e2e-key-0123456789abcdef",
        claim_reviewer_token="rev-secret-token",
    )
    from app.main import create_app

    return TestClient(create_app(settings))


def _quote_and_capture(client: TestClient, item_price: str = "80.00") -> dict:
    body = {**QUOTE_BODY, "item_price": item_price}
    r = client.post("/api/v1/quotes", json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    quote = payload["quote"]
    r = client.post(
        "/api/v1/orders",
        json={"quote_id": quote["quote_id"], "buyer_ref": "buyer_ledger", "opt_in": True},
    )
    assert r.status_code == 200, r.text
    order_id = r.json()["order_id"]
    r = client.post(f"/api/v1/orders/{order_id}/capture")
    assert r.status_code == 200, r.text
    return {"quote": quote, "order_id": order_id}


def _capture_and_refund(client: TestClient) -> dict:
    """Capture, submit a claim, and settle it to a demo refund."""
    flow = _quote_and_capture(client)
    order_id = flow["order_id"]
    r = client.post(
        "/api/v1/claims",
        data={"order_id": order_id},
        files={"photo": ("receipt.jpg", b"\x89PNG\r\n\x1a\n-ok", "image/png")},
    )
    assert r.status_code == 201, r.text
    claim_id = r.json()["claim_id"]
    resolved = client.post(
        f"/api/v1/claims/{claim_id}/resolve",
        json={"decision": "REFUND", "reason_code": "TEST"},
        headers={"X-Reviewer-Token": "rev-secret-token"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["state"] == "REFUNDED"
    return {**flow, "claim_id": claim_id, "refund": resolved.json()}


class TestTransactions:
    def test_empty_ledger_returns_empty_array(self, tmp_path):
        with _client(tmp_path) as client:
            r = client.get("/api/v1/ledger/transactions")
            assert r.status_code == 200
            assert r.json() == []

    def test_captured_order_row_matches_frozen_shape(self, tmp_path):
        with _client(tmp_path) as client:
            _quote_and_capture(client)
            rows = client.get("/api/v1/ledger/transactions").json()
            assert len(rows) == 1
            row = rows[0]
            assert set(row) == {
                "internal_order_id", "merchant_id", "item_price",
                "protection_fee", "r_final", "state", "created_at",
            }
            assert row["merchant_id"] == "mer_ledger_e2e"
            assert row["item_price"] == "80.00"          # money as strings
            assert Decimal(row["protection_fee"]) > 0    # opted-in fee line
            assert Decimal(row["r_final"]) > 0           # joined from the quote
            assert row["state"] == "CAPTURED"
            assert row["created_at"].endswith("Z") or "+" in row["created_at"]

    def test_protection_fee_row_for_opt_in_order(self, tmp_path):
        with _client(tmp_path) as client:
            _quote_and_capture(client)
            row = client.get("/api/v1/ledger/transactions").json()[0]
            fee = Decimal(row["protection_fee"])
            assert fee > 0  # opted-in orders always carry the fee line

    def test_capture_replay_never_duplicates_rows(self, tmp_path):
        with _client(tmp_path) as client:
            flow = _quote_and_capture(client)
            # capture replay (idempotent second POST)
            r = client.post(f"/api/v1/orders/{flow['order_id']}/capture")
            assert r.status_code == 200
            rows = client.get("/api/v1/ledger/transactions").json()
            assert len(rows) == 1  # one transaction per order, still
            # and the summary total stayed a single item price
            summary = client.get("/api/v1/ledger/summary").json()
            assert Decimal(summary["total_volume"]) == Decimal("80.00")

    def test_refunded_state_and_refund_ledger_consistency(self, tmp_path):
        with _client(tmp_path) as client:
            _capture_and_refund(client)
            rows = client.get("/api/v1/ledger/transactions").json()
            assert rows[0]["state"] == "REFUNDED"
            summary = client.get("/api/v1/ledger/summary").json()
            # full demo refund of the item price
            assert Decimal(summary["refunds_issued"]) == Decimal("80.00")
            assert Decimal(summary["total_volume"]) == Decimal("80.00")

    def test_pagination_window(self, tmp_path):
        with _client(tmp_path) as client:
            for price in ("10.00", "20.00", "30.00", "40.00"):
                _quote_and_capture(client, item_price=price)
            page1 = client.get(
                "/api/v1/ledger/transactions", params={"limit": 2, "offset": 0}
            ).json()
            page2 = client.get(
                "/api/v1/ledger/transactions", params={"limit": 2, "offset": 2}
            ).json()
            assert len(page1) == 2 and len(page2) == 2
            ids = [r["internal_order_id"] for r in page1 + page2]
            assert len(set(ids)) == 4  # pages never overlap

            everything = client.get("/api/v1/ledger/transactions").json()
            assert [r["internal_order_id"] for r in everything] == ids

    def test_filters_by_merchant_and_state(self, tmp_path):
        with _client(tmp_path) as client:
            _quote_and_capture(client)
            ok = client.get(
                "/api/v1/ledger/transactions",
                params={"merchant_id": "mer_ledger_e2e"},
            )
            assert ok.status_code == 200
            assert len(ok.json()) == 1
            assert client.get(
                "/api/v1/ledger/transactions",
                params={"merchant_id": "no-such-merchant"},
            ).json() == []
            captured = client.get(
                "/api/v1/ledger/transactions", params={"state": "CAPTURED"}
            )
            assert len(captured.json()) == 1
            assert client.get(
                "/api/v1/ledger/transactions", params={"state": "REFUNDED"}
            ).json() == []

    @pytest.mark.parametrize(
        "params",
        [
            {"limit": 0},
            {"limit": 201},
            {"offset": -1},
            {"state": "GOLDEN_STATE"},  # not a state-machine state
        ],
    )
    def test_invalid_parameters_are_422(self, tmp_path, params):
        with _client(tmp_path) as client:
            r = client.get("/api/v1/ledger/transactions", params=params)
            assert r.status_code == 422
            # project-wide validation error convention (app/api/errors.py)
            assert r.json()["error"]["code"] == "VALIDATION_ERROR"


class TestSummary:
    def test_empty_summary_is_all_zero(self, tmp_path):
        with _client(tmp_path) as client:
            summary = client.get("/api/v1/ledger/summary").json()
            assert set(summary) == {
                "total_volume", "protection_fees_collected",
                "refunds_issued", "net_loss_ratio",
            }
            assert Decimal(summary["total_volume"]) == 0
            assert Decimal(summary["protection_fees_collected"]) == 0
            assert Decimal(summary["refunds_issued"]) == 0
            assert Decimal(summary["net_loss_ratio"]) == Decimal("0.0000")

    def test_summary_after_capture_and_fee_retained(self, tmp_path):
        with _client(tmp_path) as client:
            flow = _quote_and_capture(client)
            fee = Decimal(flow["quote"]["protection_fee"])
            summary = client.get("/api/v1/ledger/summary").json()
            # captured item revenue is counted; the fee is collected and kept
            assert Decimal(summary["total_volume"]) == Decimal("80.00")
            assert Decimal(summary["protection_fees_collected"]) == fee
            assert Decimal(summary["refunds_issued"]) == 0
            assert Decimal(summary["net_loss_ratio"]) == Decimal("0.0000")

    def test_full_refund_yields_exact_loss_ratio(self, tmp_path):
        with _client(tmp_path) as client:
            _capture_and_refund(client)
            summary = client.get("/api/v1/ledger/summary").json()
            # 80 refund on 80 volume = 1.0000, never float-rounded
            assert Decimal(summary["net_loss_ratio"]) == Decimal("1.0000")
            # the Buyer Protection Fee is retained on refund (never repaid)
            assert Decimal(summary["protection_fees_collected"]) > 0


class TestMerchants:
    def test_trust_row_uses_phase2_oracle_after_capture(self, tmp_path):
        with _client(tmp_path) as client:
            _quote_and_capture(client)  # reputation: total=1, successful=1
            rows = client.get("/api/v1/ledger/merchants").json()
            row = next(r for r in rows if r["merchant_id"] == "mer_ledger_e2e")
            assert row["total_orders"] == 1
            assert row["successful_orders"] == 1
            assert row["verified_fraud_claims"] == 0
            # (1 + 7) / (1 + 10) = 0.7273, computed by score_reputation
            assert row["trust_score"] == "0.7273"
            assert row["m_penalty"] == "1.00"

    def test_fraud_penalty_bumps_after_verified_refund(self, tmp_path):
        with _client(tmp_path) as client:
            _capture_and_refund(client)  # closed loop: verified_fraud_claims += 1
            rows = client.get("/api/v1/ledger/merchants").json()
            row = next(r for r in rows if r["merchant_id"] == "mer_ledger_e2e")
            assert row["verified_fraud_claims"] == 1
            assert row["m_penalty"] == "1.50"
            # same Bayesian denominators: counters unchanged by the refund
            assert row["trust_score"] == "0.7273"

    def test_merchant_pagination(self, tmp_path):
        with _client(tmp_path) as client:
            for i in range(3):
                client.post(
                    "/api/v1/quotes",
                    json={**QUOTE_BODY, "merchant_id": f"mer_{i}", "item_hash": f"{i}" * 64},
                )
            rows = client.get("/api/v1/ledger/merchants").json()
            assert [r["merchant_id"] for r in rows] == ["mer_0", "mer_1", "mer_2"]
            page = client.get(
                "/api/v1/ledger/merchants", params={"limit": 1, "offset": 1}
            ).json()
            assert [r["merchant_id"] for r in page] == ["mer_1"]
