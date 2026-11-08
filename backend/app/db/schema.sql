-- PayPal Shield canonical database schema (Person 1)
-- PostgreSQL dialect, one-to-one with app/db/models.py and spec section 5.2.
-- Migrations for both PostgreSQL and SQLite via Alembic (app/db/migrations).
-- COMMENT CONSISTENCY: use "Buyer Protection Fee" / "Protection Plan";
-- legacy risk-transfer terminology is forbidden in comments (REQ-NF-05).

CREATE TABLE IF NOT EXISTS merchant_reputation (
    merchant_id           TEXT PRIMARY KEY,
    total_orders          INTEGER NOT NULL DEFAULT 0 CHECK (total_orders >= 0),
    successful_orders     INTEGER NOT NULL DEFAULT 0 CHECK (successful_orders >= 0),
    verified_fraud_claims INTEGER NOT NULL DEFAULT 0 CHECK (verified_fraud_claims >= 0),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (successful_orders <= total_orders)
);

CREATE TABLE IF NOT EXISTS risk_cache (
    image_hash     CHAR(64) NOT NULL,
    text_hash      CHAR(64) NOT NULL,
    pillars_json   JSONB NOT NULL,
    model_versions JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (image_hash, text_hash)
);

-- NOTE: quote_id is stored as TEXT (engine emits 'q_<hex>' ids, contract type string).
CREATE TABLE IF NOT EXISTS quotes (
    quote_id       TEXT PRIMARY KEY,
    merchant_id    TEXT NOT NULL REFERENCES merchant_reputation(merchant_id),
    item_hash      CHAR(64) NOT NULL,
    item_price     NUMERIC(14,2) NOT NULL,
    currency_code  CHAR(3) NOT NULL,
    protection_fee NUMERIC(14,2) NOT NULL, -- "Buyer Protection Fee" line item
    r_final        NUMERIC(5,2) NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    hmac_signature TEXT NOT NULL,
    consumed       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    internal_order_id UUID PRIMARY KEY,
    quote_id          TEXT NOT NULL REFERENCES quotes(quote_id),
    paypal_order_id   TEXT UNIQUE,
    paypal_capture_id TEXT UNIQUE,
    merchant_id       TEXT NOT NULL REFERENCES merchant_reputation(merchant_id),
    buyer_ref         TEXT NOT NULL,
    opted_in          BOOLEAN NOT NULL,
    item_price        NUMERIC(14,2) NOT NULL,
    protection_fee    NUMERIC(14,2) NOT NULL DEFAULT 0,
    currency_code     CHAR(3) NOT NULL,
    state             TEXT NOT NULL,
    dispute_active    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_orders_merchant_id ON orders (merchant_id);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_webhook_events_type ON webhook_events (event_type);

CREATE TABLE IF NOT EXISTS claims (
    claim_id            UUID PRIMARY KEY,
    internal_order_id   UUID NOT NULL REFERENCES orders(internal_order_id),
    state               TEXT NOT NULL,
    claim_amount        NUMERIC(14,2) NOT NULL,
    currency_code       CHAR(3) NOT NULL,
    received_image_hash CHAR(64),
    discrepancy         NUMERIC(5,4),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (internal_order_id)
);
CREATE INDEX IF NOT EXISTS ix_claims_state ON claims (state);

CREATE TABLE IF NOT EXISTS claim_events (
    id           BIGSERIAL PRIMARY KEY,
    claim_id     UUID NOT NULL REFERENCES claims(claim_id),
    from_state   TEXT,
    to_state     TEXT NOT NULL,
    actor        TEXT NOT NULL,
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_claim_events_claim ON claim_events (claim_id);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id     TEXT PRIMARY KEY,  -- PayPal refund id
    claim_id      UUID NOT NULL UNIQUE REFERENCES claims(claim_id),
    amount        NUMERIC(14,2) NOT NULL, -- item price only; the protection fee is retained
    currency_code CHAR(3) NOT NULL,
    status        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id                BIGSERIAL PRIMARY KEY,
    internal_order_id UUID NOT NULL REFERENCES orders(internal_order_id),
    kind              TEXT NOT NULL CHECK (kind IN ('MERCHANT_REVENUE', 'PROTECTION_FEE', 'REFUND')),
    amount            NUMERIC(14,2) NOT NULL,
    currency_code     CHAR(3) NOT NULL,
    at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ledger_order_id ON ledger_entries (internal_order_id);
