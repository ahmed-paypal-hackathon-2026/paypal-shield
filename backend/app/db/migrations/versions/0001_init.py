"""init - create the 9 canonical PayPal Shield tables

Revision ID: 0001_init
Revises: None
Create Date: 2026-11-05

Built directly from app.db.models.Base.metadata so the migration can never
drift from the canonical ORM definitions (quotes/claims/refunds/ledger and
merchant_reputation are payment-critical).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.models import Base

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
