"""Declarative base shared by every PayPal Shield table (Person 1)."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Parent for all ORM models. Money uses ``MoneyAmount`` (no float)."""
