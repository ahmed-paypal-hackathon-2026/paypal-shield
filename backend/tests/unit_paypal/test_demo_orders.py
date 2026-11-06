"""Demo-mode order flow end-to-end (Person 1).

No PayPal credentials + DEMO_DETERMINISTIC_MODE -> router chain fully
exercised over TestClient: quotes -> orders -> capture, all money strings,
deterministic demo ids, replay safe.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.db.models import LedgerEntryRow, MerchantReputationRow, OrderRow


def _client(tmp_path) -> TestClient:
    settings = Settings(
        _env_file=None,
        engine_impl="stub",
        demo_deterministic_mode=True,   # -> DemoPayPalTransport (no creds set)
        paypal_client_id="",
        paypal_client_secret="",
        sqlite_url=f"sqlite+aiosqlite:///{tmp_path}/demo.db",
        quotes_hmac_key="demo-e2e-key-0123456789abcdef",
    )
    app = create_app(settings)
    return TestClient(app)


QUOTE_BODY = {
    "merchant_id": "mer_demo_e2e",
    "item_hash": "a" * 64,
    "item_title": "Demo Drone",
    "item_price": "120.00",
    "currency_code": "USD",
}


def test_full_demo_chain_quote_order_capture(tmp_path):
    with _client(tmp_path) as client:
        # 1. quote
        r = client.post("/api/v1/quotes", json=QUOTE_BODY)
        assert r.status_code == 200
        quote = r.json()["quote"]
        assert quote is not None and quote["protection_fee"] == "2.70"

        # 2. create order (opt-in)
        r = client.post(
            "/api/v1/orders",
            json={"quote_id": quote["quote_id"], "buyer_ref": "buyer_001", "opt_in": True},
        )
        assert r.status_code == 200
        order = r.json()
        assert order["order_id"].startswith("DEMO")
        assert order["status"] == "ORDER_CREATED"
        assert order["total"] == "122.70"
        assert order["currency_code"] == "USD"

        # 3. capture
        r = client.post(f"/api/v1/orders/{order['order_id']}/capture")
        assert r.status_code == 200
        capture = r.json()
        assert capture["paypal_capture_id"].startswith("DEMOCAP")
        assert capture["status"] == "COMPLETED"

        # internal side effects are visible in the DB
        app = client.app
        factory = app.state.session_factory
        import asyncio

        async def check():
            async with factory() as s:
                row = (
                    await s.execute(
                        select(OrderRow).where(
                            OrderRow.paypal_order_id == order["order_id"]
                        )
                    )
                ).scalar_one()
                assert row.state == "CAPTURED"
                assert row.opted_in is True
                rows = (await s.execute(
                    select(LedgerEntryRow).where(
                        LedgerEntryRow.internal_order_id == row.internal_order_id
                    )
                )).scalars().all()
                kinds = {r2.kind: str(r2.amount) for r2 in rows}
                assert kinds == {
                    "MERCHANT_REVENUE": "120.00",
                    "PROTECTION_FEE": "2.70",
                }
                rep = await s.get(MerchantReputationRow, row.merchant_id)
                assert rep.total_orders == 1 and rep.successful_orders == 1

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(check())


def test_demo_replay_of_order_and_capture(tmp_path):
    with _client(tmp_path) as client:
        quote = client.post("/api/v1/quotes", json=QUOTE_BODY).json()["quote"]
        first = client.post(
            "/api/v1/orders",
            json={"quote_id": quote["quote_id"], "buyer_ref": "b1", "opt_in": True},
        ).json()
        # identical create -> same response (idempotent), and the playthrough
        next = client.post(
            "/api/v1/orders",
            json={"quote_id": quote["quote_id"], "buyer_ref": "b1", "opt_in": True},
        ).json()
        assert next == first
        cap1 = client.post(f"/api/v1/orders/{first['order_id']}/capture").json()
        cap2 = client.post(f"/api/v1/orders/{first['order_id']}/capture").json()
        assert cap1 == cap2


def test_demo_consumed_quote_conflict(tmp_path):
    with _client(tmp_path) as client:
        quote = client.post("/api/v1/quotes", json=QUOTE_BODY).json()["quote"]
        client.post(
            "/api/v1/orders",
            json={"quote_id": quote["quote_id"], "buyer_ref": "b1", "opt_in": True},
        )
        # quote is consumed: a different buyer cannot order with it
        r = client.post(
            "/api/v1/orders",
            json={"quote_id": quote["quote_id"], "buyer_ref": "b2", "opt_in": True},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "QUOTE_CONSUMED"
