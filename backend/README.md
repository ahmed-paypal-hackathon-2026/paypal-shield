# PayPal Shield — Backend

FastAPI backend, owned by **Person 1 (Backend, Database and PayPal API Lead)**.
Everything here is driven by the frozen API contract in `shared/openapi.json` —
the contract is the source of truth; the live app routes are byte-aligned to it
(verified in CI and in `app/main.py`'s `_align_openapi_with_contract`).

> Ownership boundaries: see the root `README.md`. Backend code lives in
> `app/{api,db,services}`, `app/main.py`, `app/config.py`, `requirements.txt`
> and the `tests/unit_paypal` / `tests/unit_db` suites. `app/schemas/*.py`,
> `app/ai/**`, `tests/{integration,security,unit_ai}`, `shared/`, CI and Docker
> are other teammates' — do not edit them.

---

## What the backend does

1. **Quotes** — pre-computed risk pillars (from `app/ai/`, P2's engine) are
   converted by a deterministic Decimal actuarial engine into a bounded risk
   score and a capped Buyer Protection Fee, returned as an HMAC-SHA256-signed
   quote with a TTL. Never trusts the client: fees are computed and signed
   server-side only.
2. **Orders** — creates a PayPal Orders v2 order with the fee as an explicit
   line item, re-verifies the signed quote at order creation, and captures.
   Capture triggers ledger entries and reconciliation.
3. **Claims + Arbiter** — buyer-submitted counterfeit claims with photo
   evidence (magic-byte validated, decompression-limited). A closed-loop
   pipeline walks `SUBMITTED → EVIDENCE_CHECKED → AUTO_APPROVED /
   MANUAL_REVIEW / REJECTED → REFUNDED`, with velocity limits, a payout
   ceiling, and idempotent refunds via the PayPal Captures Refund API.
   Verified outcomes feed back into merchant reputation, raising the penalty
   multiplier for the next quote.
4. **Webhooks** — PayPal webhook receiver; events are verified and
   reconciled against persisted state, idempotent under retries.
5. **Ledger API** — read-only GETs over transactions, merchant trust rows and
   a money summary. Merchant trust is derived on read via P2's
   `app/ai/reputation.py` (`score_reputation`), never stored.
6. **Stream API** — Server-Sent Events feed (`transaction` / `claim` /
   `merchant` events) that reflects **persisted state only**: no event is
   emitted before its DB commit, and retried operations never emit duplicates.

## API surface (12 frozen routes, contract v1.0.0)

| Route | Method | Phase | Purpose |
| --- | --- | --- | --- |
| `/api/v1/quotes` | POST | 1 | Signed fee quote with TTL |
| `/api/v1/orders` | POST | 1 | Create PayPal order + fee line item |
| `/api/v1/orders/{id}/capture` | POST | 1 | Capture, reconcile, emit ledger events |
| `/api/v1/claims` | POST | 3 | Submit claim (photo evidence) |
| `/api/v1/claims/{id}` | GET | 3 | Claim status |
| `/api/v1/claims/{id}/resolve` | POST | 3 | Reviewer decision (token-gated) |
| `/api/v1/ledger/transactions` | GET | 4 | Join of orders+quotes (fee, capture, amounts) |
| `/api/v1/ledger/merchants` | GET | 4 | Derived merchant trust rows |
| `/api/v1/ledger/summary` | GET | 4 | Money totals by kind (exact Decimal math) |
| `/api/v1/stream` | GET | 4 | SSE event feed |
| `/api/v1/webhooks/paypal` | POST | 3 | PayPal webhook receiver |
| `/api/v1/health` | GET | 0 | Health/liveness probe |

Interactive docs: `http://localhost:8000/docs` served from the app's OpenAPI.

## Running locally (no Docker)

```bash
# from the repository root
pip install -r backend/requirements.txt
cp .env.example .env            # at minimum set QUOTES_HMAC_KEY (32+ chars)

# serve
python -m uvicorn app.main:app --app-dir backend --reload
# or: Set-Location backend; uvicorn app.main:app --reload   (PowerShell)
```

- API at `http://localhost:8000`, docs at `/docs`, health at
  `/api/v1/health`.
- Database defaults to SQLite at the repo root (`shield.db`); set
  `DATABASE_URL` for Postgres in deployment.
- **Sandbox guard**: the app refuses to boot against any non-Sandbox PayPal
  base URL unless `ALLOW_NON_SANDBOX=true` is set explicitly.

## Running with Docker

```bash
docker compose up --build
# API :8000 (health /api/v1/health), Postgres :5432, storefront :5173
```

## Configuration (env only — no secrets in code or logs)

All settings live in `app/config.py` (pydantic-settings, `.env` supported).
The important ones:

| Variable | Default | Notes |
| --- | --- | --- |
| `QUOTES_HMAC_KEY` | — | **Required.** 32+ chars; signs quotes |
| `QUOTES_HMAC_KEY_PREVIOUS` | — | Accepted during key rotation window |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | — | Without them, `DEMO_DETERMINISTIC_MODE` covers the demos |
| `PAYPAL_WEBHOOK_ID` | — | Required for live webhook verification |
| `PAYPAL_API_BASE` | Sandbox URL | Non-Sandbox refused unless `ALLOW_NON_SANDBOX=true` |
| `DATABASE_URL` / `SQLITE_URL` | `shield.db` | Postgres in deployment, SQLite locally |
| `CLAIM_REVIEWER_TOKEN` | — | Token for claim `/resolve` |
| `CLAIM_*` knobs | see config | Window, payout cap, velocity limits — recalibratable without code changes |
| `STREAM_HEARTBEAT_SECONDS` | `15` | SSE keep-alive cadence |
| `ENGINE_IMPL` | `app_ai` | `stub` = deterministic fallback for tests |
| `ALLOWED_ORIGINS` | localhost:5173/4173 | CORS allowlist |

Secrets are redacted from logs by `SecretRedactingFilter` — access tokens,
HMAC signatures and Bearer headers never reach a log line.

## Project layout

```
backend/
  app/
    api/          # HTTP layer: quotes, orders, claims, webhooks, ledger, stream
    db/           # async engine/session, models, repositories
    services/     # business flows: orders_flow, claims, ledger, events (EventBus)
    ai/           # P2's risk pillars + actuarial engine (import-only, do not edit)
    schemas/      # frozen contract mirrors (do not edit)
    config.py     # env-only settings + sandbox guard + log redaction
    main.py       # app factory, router wiring, OpenAPI alignment
  tests/
    unit_paypal/  # P1's API/service tests
    unit_db/      # P1's DB tests
    unit_ai/      # P2 (do not edit)
    integration/, security/  # P4 (do not edit)
  requirements.txt
  Dockerfile
```

### Event flow (Stream API)

`app/services/events.py` owns a process-wide `EventBus`. Emit points stage
events on the SQLAlchemy session and publish them **only after commit**
(`commit_and_emit`), so the SSE feed never announces un-persisted state and
retried idempotent operations never double-emit. Claim-pipeline updates are
emitted synchronously before background tasks are scheduled to keep frame
ordering correct. The full details —
[`docs/phase4-handoff.md`](../docs/phase4-handoff.md) cover the event
catalog, SSE reconnect semantics (`Last-Event-ID` replay) and the frontend
integration recipe.

## Tests

```bash
# from the repository root
# full suite (the Compose integration test requires Docker)
python -m pytest backend/tests -q -p no:warnings

# local verification when Docker is not running
python -m pytest backend/tests -q -p no:warnings --ignore=backend/tests/integration/test_compose_health.py
# verified result: 314 passed, 1 skipped
```

- `tests/unit_paypal/test_ledger_api.py` + `test_stream_api.py` — Phase 4
  (33 tests): ledger shapes/filters/pagination, exact-money summary math,
  trust oracle, SSE framing, replay, heartbeats, disconnect cleanup.
- Contract-drift check: the 12 frozen routes and the Phase 4 response schemas
  are compared against `shared/openapi.json` — all must match byte-exactly.

## Known gotchas

- **Python 3.15 removed `asyncio.isawaitable`** — use
  `inspect.isawaitable` (it was silently swallowing every stream event).
- Money is **exact `Decimal` everywhere**; never coerce amounts through
  float (SQLite stores Decimal as string — summary sums aggregate in Python).
- Stream heartbeat responses are infinite — SSE tests must consume with a
  bounded reader, not unbounded `iter_lines()`.
- PowerShell: no heredocs / nested-quote `python -c`; set
  `$env:PYTHONPATH="backend"` for direct app imports.

## Open team decisions (tracked in the handoff doc)

1. Auth/scope for Stream + Ledger — the frozen contract has no
   `securitySchemes`, so implementation is contract-faithful (unauthenticated);
   needs a contract-level decision.
2. Listing-image source in the claims flow (placeholder today).
3. Duplicate-claim replay semantics.
