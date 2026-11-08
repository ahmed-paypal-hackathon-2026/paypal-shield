# Phase 4 — Ledger API & Stream API — Frontend Handoff

Everything below reflects the ACTUAL running implementation (not invented
payloads). Freeze source: `shared/openapi.json`. Verify against
`GET /openapi.json` on the running server.

## 1. Endpoints at a glance

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/ledger/transactions` | Order transaction rows (most recent first) |
| GET | `/api/v1/ledger/merchants`   | Merchant trust registry (Bayesian trust) |
| GET | `/api/v1/ledger/summary`     | Pinned grid totals |
| GET | `/api/v1/stream`             | SSE live feed (`text/event-stream`) |

All are JSON (except the stream) and require **no headers/body**.

### Contract-fidelity notes (documented deviations, additive only)

- The frozen contract declares NO query parameters. The server accepts
  **optional** validated query params so the board can page/filter; defaults
  keep a plain `GET` identical to a contract-only call.
- The frozen contract declares no path variable names on `/stream`; the live
  app `/openapi.json` now matches the frozen contract byte-for-byte for
  operationIds, descriptions, 200 response schemas and the three component
  schemas (`TransactionRow`, `MerchantTrustRow`, `LedgerSummary`).
- Streams: topic filtering is an **additive** optional query param
  (`topics=transaction,claim` — invalid → 422). No auth scheme exists at
  contract level (see Section 6).
- There is deliberately NO per-entry/per-order/per-claim detail ledger route:
  the frozen contract has none, and `GET /claims/{id}` already reads a claim.

## 2. Ledger responses (exact shapes)

Money serializes as **decimal strings** exactly like the mock
(`frontend/src/api/mock.ts`): `"120.00"`-style strings, never JSON numbers.

### GET /api/v1/ledger/transactions

Optional query params: `limit` (int 1–200, default 100), `offset` (int ≥ 0,
default 0), `merchant_id` (string), `state` (enum: `QUOTED | ORDER_CREATED |
CAPTURED | CAPTURE_FAILED | PENDING_RECONCILIATION | PARTIALLY_REFUNDED |
REFUNDED`). Ordering is `created_at DESC` with `internal_order_id` as
tie-breaker (deterministic pages).

```http
GET /api/v1/ledger/transactions?limit=2&state=CAPTURED HTTP/1.1

200 OK
content-type: application/json
cache-control: no-store
```

```json
[
  {
    "internal_order_id": "0f2f0a1e-...",
    "merchant_id": "mer_stream_e2e",
    "item_price": "80.00",
    "protection_fee": "0.40",
    "r_final": "7.80",
    "state": "CAPTURED",
    "created_at": "2026-11-06T12:31:04.123456+00:00"
  }
]
```

Empty DB → `[]` (not an error). Invalid params →
`422 {"error": {"code": "VALIDATION_ERROR", "message": "request failed validation"}}`.

### GET /api/v1/ledger/merchants

Optional `limit`/`offset` (same bounds). Trust is derived on read via the
Phase 2 scorer: `trust_score = (successful_orders + 7) / (total_orders + 10)`,
`m_penalty = 1.00 | 1.50 | 2.00` for `0 | 1 | 2+` verified fraud claims.

```json
[
  {
    "merchant_id": "mer_stream_e2e",
    "total_orders": 1,
    "successful_orders": 1,
    "verified_fraud_claims": 0,
    "trust_score": "0.7273",
    "m_penalty": "1.00"
  }
]
```

### GET /api/v1/ledger/summary

```json
{
  "total_volume": "80.00",
  "protection_fees_collected": "0.40",
  "refunds_issued": "80.00",
  "net_loss_ratio": "1.0000"
}
```

`net_loss_ratio = refunds_issued / total_volume`, quantized to 4 decimal
places (`0.0000` when there is no volume yet). Totals are summed with exact
Decimal arithmetic (never binary floats) directly from the persisted
`ledger_entries`.

## 3. Stream connection instructions

```ts
// frontend/src/api/stream.ts already matches this; no changes required
const source = new EventSource(`${base}/api/v1/stream`);
source.addEventListener("transaction", (ev) => handler(JSON.parse(ev.data)));
source.addEventListener("claim", (ev) => handler(JSON.parse(ev.data)));
source.addEventListener("merchant", (ev) => handler(JSON.parse(ev.data)));
```

- Content-Type: `text/event-stream`; `Cache-Control: no-store`;
  `X-Accel-Buffering: no` (proxy pass-through).
- First line is `retry: 3000` — the browser `EventSource` uses it for
  automatic reconnect backoff.
- Keep-alives: `: ping` comment every `STREAM_HEARTBEAT_SECONDS` (default 15 s)
  keep proxies alive; they carry no event semantics and can be ignored.
- Optional additive filter: `GET /api/v1/stream?topics=transaction,claim`
  (invalid value → 422). Default delivers all three topics.

### Frame format (per event)

```
event: transaction
id: 7
data: {"topic":"transaction","action":"add","record":{"internal_order_id":"...","merchant_id":"...","item_price":"80.00","protection_fee":"0.00","r_final":"7.80","state":"ORDER_CREATED","created_at":"..." }}

```

`data` is exactly `{topic, action, record}` per the frozen contract.
`action` is `add | update` (merchant emits only `update`, matching the
frontend `StreamEnvelope` union).

### Event catalog (persisted state only — never speculative)

| # | Trigger | event | action | record type | record.state |
|---|---|---|---|---|---|
| 1 | `POST /orders` persists a NEW order | `transaction` | `add` | `TransactionRow` | `ORDER_CREATED` |
| 2 | `POST /orders/{id}/capture` persists CAPTURED | `transaction` | `update` | `TransactionRow` | `CAPTURED` |
| 3 | Capture retried and replayed | *none* | — | — | (no duplicate) |
| 4 | Capture fails / limbo state persisted | `transaction` | `update` | `TransactionRow` | `CAPTURE_FAILED` / `PENDING_RECONCILIATION` |
| 5 | `POST /claims` persists a NEW claim | `claim` | `add` | `ClaimRecord` | `SUBMITTED` |
| 6 | Arbiter decision persisted (pipeline) | `claim` | `update` | `ClaimRecord` | `AUTO_APPROVED` / `MANUAL_REVIEW` / `REJECTED` |
| 7 | Refund settled (`_finalize_refunded`) | `claim` | `update` | `ClaimRecord` | `REFUNDED` |
| 7b | …same commit updates the order | `transaction` | `update` | `TransactionRow` | `REFUNDED` / `PARTIALLY_REFUNDED` |
| 7c | …same commit bumps merchant fraud | `merchant` | `update` | `MerchantTrustRow` | new `verified_fraud_claims`+`m_penalty` |
| 8 | Webhook `PAYMENT.CAPTURE.COMPLETED` reconciles | `transaction` | `update` | `TransactionRow` | `CAPTURED` |

Order-in-behavior guarantees:

- Events are published **only after** their producing transaction commits —
  the stream can never show an unpersisted capture/refund.
- Retried/idempotent operations emit **zero** extra events (capture replay,
  claim replay, already-REFUNDED claims short-circuit before enqueueing).
- `claim/add` always precedes the pipeline's `claim/update` for that claim.

### Reconnection & replay

On reconnect, send the last seen `id` as the `Last-Event-ID` header (browsers
do this automatically for `EventSource`):

```http
GET /api/v1/stream
Last-Event-ID: 7
```

The server replays every event with `id > cursor` first, deduped against
events arriving live (high-water mark) — a reconnect loses nothing and
receives no duplicates. The replay window covers the last 500 events
(in-memory history). For full history, read the ledger/claims endpoints.

## 4. Error handling

Project-wide error envelope (`app/api/errors.py`):

```json
422 / 400 / 409 / 404 → {"error": {"code": "...", "message": "..."}}
```

Ledger/stream-specific codes observed: `VALIDATION_ERROR` (422, invalid query
params). The stream route itself never fails mid-connection for business
reasons; disconnects are normal client lifecycle.

## 5. Quick verification script (curl / browser)

1. `GET http://localhost:8000/api/v1/ledger/summary` → all zero, `0.0000`.
2. `curl -N http://localhost:8000/api/v1/stream` → `retry: 3000` then `: ping`
   heartbeats.
3. Run the demo chain (quote → order → capture → claim → reviewer REFUND) and
   watch the stream emit frames in the order of Section 3's table.
4. Reconnect with `Last-Event-ID: <last id>` → replay of just the newer events.

## 6. Open item requiring a team decision (recorded conflict)

The task asked for per-user event isolation and authenticated streaming. The
frozen contract declares **no securitySchemes and no per-user variant** of
`/api/v1/stream` (it is a live-ops board feed), and this app has no identity
system (no user table / bearer tokens anywhere in Phase 1–3). Implemented
contract-faithfully (single public feed). If Person 4 adds auth + a
buyer-scope variant to the contract, `enqueue_event` calls stay unchanged;
only the dependency plumbing on the route changes. Same applies to the three
ledger GETs (aggregates, no buyer-identifiable fields).
