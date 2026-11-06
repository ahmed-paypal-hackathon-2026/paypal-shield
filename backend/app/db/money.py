"""Portable money type (Person 1).

``MoneyAmount`` stores money as ``NUMERIC`` on PostgreSQL and as a canonical
fixed-point string on SQLite, and always yields ``decimal.Decimal`` in both
directions. No binary float ever represents money (spec section 1.4,
"deterministic money").

Why not plain ``sa.Numeric`` on SQLite: SQLite has no NUMERIC storage class
for Decimal, and SQLAlchemy's default conversion would coerce values through
``float``. A string keeps every cent exact.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import Numeric, String
from sqlalchemy.types import TypeDecorator

_DIALECTS_WITH_NATIVE_NUMERIC = frozenset({"postgresql", "mysql", "mssql", "oracle"})


class MoneyAmount(TypeDecorator):
    """NUMERIC(precision, scale) on native dialects; string elsewhere."""

    impl = String
    cache_ok = True

    def __init__(self, precision: int = 14, scale: int = 2) -> None:
        super().__init__()
        self.precision = precision
        self.scale = scale

    def load_dialect_impl(self, dialect):
        if dialect.name in _DIALECTS_WITH_NATIVE_NUMERIC:
            return dialect.type_descriptor(
                Numeric(self.precision, self.scale, asdecimal=True)
            )
        # SQLite and any other dialect: canonical fixed-point string.
        return dialect.type_descriptor(String(self.precision + self.scale + 4))

    @staticmethod
    def to_decimal(value: Any) -> Decimal:
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:  # pragma: no cover - defensive
            raise ValueError(f"not a valid money value: {value!r}") from exc

    def process_bind_param(self, value: Optional[Any], dialect) -> Optional[Any]:
        if value is None:
            return None
        dec = self.to_decimal(value)
        if dialect.name in _DIALECTS_WITH_NATIVE_NUMERIC:
            return dec  # DBAPI handles Decimal natively on NUMERIC
        # Canonical fixed-point string, never exponent form.
        text = format(dec, "f")
        return text

    def process_result_value(self, value: Optional[Any], dialect) -> Optional[Decimal]:
        if value is None:
            return None
        dec = self.to_decimal(value)
        if dialect.name in _DIALECTS_WITH_NATIVE_NUMERIC:
            # Guarantee scale in Python (DB may return more decimals than scale).
            q = Decimal(1).scaleb(-self.scale)
            return dec.quantize(q, rounding="ROUND_HALF_UP")
        return dec
