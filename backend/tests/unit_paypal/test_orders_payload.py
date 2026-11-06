"""Order payload composer (Person 1).

The strict Decimal equality
    amount.value == item_total == Sigma(unit_amount x quantity)
is enforced BEFORE any HTTP call. Zero-decimal currencies emit no decimals.
Request-ids are deterministic uuid5 derivations.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.paypal.errors import OrderPayloadError
from app.services.paypal.orders import (
    FEE_ITEM_NAME,
    FEE_ITEM_SKU,
    build_order_payload,
    format_amount,
    request_id_for_order,
    request_id_for_quote,
    request_id_for_refund,
)


class TestFormatAmount:
    @pytest.mark.parametrize(
        "value,currency,expected",
        [
            (Decimal("120.00"), "USD", "120.00"),
            (Decimal("120.00"), "JPY", "120"),   # zero-decimal: no fraction
            (Decimal("8000"), "JPY", "8000"),
            (Decimal("35.4"), "HUF", "35"),      # zero-decimal rounds HALF_UP
            (Decimal("7.995"), "TWD", "8"),
            (Decimal("0.475"), "USD", "0.48"),   # 2-dp HALF_UP
            (Decimal("1E+2"), "USD", "100.00"),  # never exponent form
            (Decimal("0"), "USD", "0.00"),
            (Decimal("0"), "JPY", "0"),
        ],
    )
    def test_canonical_values(self, value, currency, expected):
        assert format_amount(value, currency) == expected

    def test_money_is_never_float(self):
        out = format_amount(Decimal("0.1"), "USD")
        assert out == "0.10"
        with pytest.raises(Exception, match="not a valid money"):
            format_amount("bad", "USD")  # type: ignore[arg-type]


class TestRequestIds:
    def test_deterministic_per_action(self):
        a1 = request_id_for_quote("q_1")
        a2 = request_id_for_quote("q_1")
        b = request_id_for_order("5OY1")
        c = request_id_for_refund("claim-9")
        assert a1 == a2, "same quote -> same create request id"
        assert a1 != b != c

    def test_distinct_quotes_distinct_ids(self):
        assert request_id_for_quote("q_1") != request_id_for_quote("q_2")

    def test_action_qualified_ids_cannot_collide(self):
        # Even identical inputs across action types never collide, because
        # the uuid5 message encodes the action prefix.
        raw = "5OY1"
        assert request_id_for_order(raw) != request_id_for_refund(raw)
        assert request_id_for_quote(raw) != request_id_for_order(raw)


class TestPayloadInvariant:
    def _payload(self, item="120.00", fee="2.70", currency="USD", **kw):
        return build_order_payload(
            paypal_reference_id=kw.pop("ref", "int-order-1"),
            item_price=Decimal(item),
            item_name=kw.pop("name", "Listing abc123"),
            currency_code=currency,
            protection_fee=(Decimal(fee) if fee is not None else None),
            **kw,
        )

    def test_happy_path_with_fee_line(self):
        p = self._payload()
        unit = p["purchase_units"][0]
        assert p["intent"] == "CAPTURE"
        assert unit["amount"]["value"] == "122.70"
        assert unit["amount"]["breakdown"]["item_total"]["value"] == "122.70"
        assert unit["custom_id"] == "int-order-1"
        items = unit["items"]
        assert len(items) == 2
        # first: the good; second: THE fee line (explicit DIGITAL_GOODS)
        assert items[0]["name"] == "Listing abc123"
        assert items[0]["category"] == "PHYSICAL_GOODS"
        fee = items[1]
        assert fee["name"] == FEE_ITEM_NAME == "Buyer Protection Fee (PayPal Shield)"
        assert fee["sku"] == FEE_ITEM_SKU == "SHIELD-FEE"
        assert fee["category"] == "DIGITAL_GOODS"
        assert fee["unit_amount"]["value"] == "2.70"
        assert fee["quantity"] == "1"

    def test_no_fee_when_not_opted_in(self):
        p = self._payload(fee=None)
        unit = p["purchase_units"][0]
        assert unit["amount"]["value"] == "120.00"
        assert len(unit["items"]) == 1

    def test_zero_fee_treated_as_no_fee(self):
        p = self._payload(fee="0.00")
        assert len(p["purchase_units"][0]["items"]) == 1

    def test_zero_decimal_currency_paypal_math(self):
        p = self._payload(item="8000", fee="35", currency="JPY")
        unit = p["purchase_units"][0]
        assert unit["amount"]["value"] == "8035"
        assert unit["amount"]["breakdown"]["item_total"]["value"] == "8035"
        assert unit["items"][1]["unit_amount"]["value"] == "35"

    def test_fee_at_or_above_item_price_is_rejected_pre_send(self):
        with pytest.raises(OrderPayloadError, match="protection_fee"):
            self._payload(item="2.70", fee="2.70")
        with pytest.raises(OrderPayloadError, match="protection_fee"):
            self._payload(item="1.00", fee="5.00")

    def test_zero_decimal_fractional_fee_is_rejected(self):
        # Rounding a fractional fee on a zero-decimal currency breaks the
        # rendered equality -> must refuse BEFORE PayPal is contacted.
        with pytest.raises(OrderPayloadError):
            self._payload(item="8000", fee="35.4", currency="JPY")

    def test_item_wo_total_mismatch_impossible_by_construction(self):
        # Whatever the inputs, a valid composer output must satisfy the
        # parse-back equality: value == item_total == sum(lines).
        for price, fee in [("1.00", "0.25"), ("59.99", "0.60"), ("100.00", None)]:
            p = self._payload(item=price, fee=fee)
            unit = p["purchase_units"][0]
            value = Decimal(unit["amount"]["value"])
            total = Decimal(unit["amount"]["breakdown"]["item_total"]["value"])
            lines = sum(
                Decimal(i["unit_amount"]["value"]) * int(i["quantity"])
                for i in unit["items"]
            )
            assert value == total == lines
