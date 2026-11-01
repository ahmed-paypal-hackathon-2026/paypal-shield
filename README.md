# PayPal Shield

Real-Time Risk Evaluation and Buyer Protection Fee Layer for PayPal Checkout.

A specialized multi-model AI pipeline pre-computes five risk pillars and caches
them by image SHA-256. A deterministic actuarial engine (`decimal.Decimal`,
`ROUND_HALF_UP`), fully isolated from AI outputs, converts those pillars into a
bounded risk score and a capped **Buyer Protection Fee**. The fee is quoted
with an HMAC-SHA256 signature, added as an explicit line item to a PayPal
Orders v2 order, and reconciled in a merchant reputation ledger. A closed-loop
Claim Arbiter handles post-checkout counterfeit claims and triggers idempotent
refunds through the PayPal Captures Refund API, then feeds the verified
outcome back into merchant reputation.

> The fee is a platform service fee for the **Protection Plan** — it is not a
> regulated underwriting product. Commercial deployment requires a licensed
> underwriter partnership; the hackathon build simulates the risk pool with an
> internal ledger.

## Repository layout and ownership

| Path | Owner |
| --- | --- |
| `backend/app/{api,db,services}` | P1 — Backend, Database and PayPal API Lead |
| `backend/app/ai/**`, `backend/tests/unit_ai` | P2 — AI Pipeline and Actuarial Engine Lead |
| `frontend/src/**` (except `types/contracts.ts`), `frontend/package.json`, `frontend/tailwind.config.ts` | P3 — Frontend, Checkout UX and AG Grid Lead |
| Contracts (`backend/app/schemas/contracts.py`, `frontend/src/types/contracts.ts`, `shared/openapi.json`), `shared/fixtures`, CI, Docker, Render, seeds, `backend/tests/{integration,security}` | P4 — DevOps, Security, QA and Integration Lead |

Contract files are frozen at version 1.0.0 and can only change with a version
bump and sign-off from all owners (CI enforces OpenAPI + TypeScript sync).

## Quickstart (Docker Compose)

```bash
cp .env.example .env          # fill PayPal Sandbox credentials if you have them
docker compose up --build
```

- Storefront: http://localhost:5173
- API: http://localhost:8000 — health at `/api/v1/health`
- Postgres: localhost:5432 (`shield`/`shield`)

Without PayPal credentials the storefront runs in **sandbox demo mode**
(`VITE_ALLOW_MOCK=true`): the seeded mock client reproduces the three verified
scenarios and no checkout call leaves the machine.

## Local development without Docker

Frontend (also works standalone against the mock client):

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
npm run lint       # eslint + tsc
npm test           # vitest
```

Backend is implemented by Person 1; the API contract lives in
`shared/openapi.json`.

## Verification suite (P4)

```bash
python scripts/seed_demo.py --dry-run     # print the seeding plan
python scripts/seed_demo.py               # seed demo scenarios against :8000
python -m pytest backend/tests -q         # contract, security, smoke suites
python .github/scripts/validate_openapi.py          # frozen contract shape
python .github/scripts/terminology_lint.py          # REQ-NF-05 wording guard
node .github/scripts/check_ts_contract.mjs          # TS <-> OpenAPI sync
```

CI (`.github/workflows/ci.yml`) runs: frontend lint/typecheck/test/build,
backend ruff + pytest, contract enforcement (OpenAPI validation + drift diff +
TS sync + terminology lint), gitleaks secret scan, compose config check, and
Docker image builds.

## Demo scenarios (deterministic, spec §6.3)

| Scenario | Closing risk | Gate | Fee |
| --- | --- | --- | --- |
| Trusted merchant (200 orders / 190 ok / 0 fraud, $120.00) | 7.80% | COVERAGE_AVAILABLE | $0.47 |
| AI-Generated Scam listing (new merchant 2/2, $80.00) | 70.25% | COVERAGE_AVAILABLE | $2.81 |
| Repeat offender (40 orders / 30 ok / 2 fraud, $60.00) | 81.00% (marketplace offender penalty applied) | BLOCKED_HIGH_RISK | none — checkout proceeds without coverage |

In the storefront, the **Risk Simulator** drawer swaps between the three in one
click. On the Claims Portal, upload a photo of the received item for a captured
order: the claim walks SUBMITTED → EVIDENCE_CHECKED → AUTO_APPROVED /
MANUAL_REVIEW / REJECTED → REFUNDED live over SSE, refunds bump
`verified_fraud_claims`, and the next quote on that merchant reflects the
higher penalty multiplier.

## Deployment (Render)

`render.yaml` declares the blueprint: managed Postgres, the FastAPI API as a
Docker web service (health path `/api/v1/health`), and the storefront as a
static site with an SPA rewrite. Set `PAYPAL_CLIENT_ID`,
`PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` at the account or environment
level; `QUOTES_HMAC_KEY` is generated on first sync. The API **refuses to
boot** against a non-Sandbox PayPal base URL unless `ALLOW_NON_SANDBOX=true`.

## Security posture

- Secrets only via environment variables; never logged or returned by any API.
- SSRF-hardened server-side fetcher (HTTPS only, port 443, pinned DNS,
  private/metadata ranges blocked, redirects re-validated, size and
  content-type allowlists).
- Claim uploads validated by magic bytes and decoded under a decompression
  limit.
- Rate limits and security headers; secret scanning in CI.
- Never trust the client: amounts are quoted server-side, signed, and
  re-verified at order creation and capture.

## Phase plan status

- [x] Phase 0 — Contract freeze (P4): contracts, schema files, OpenAPI skeleton, repo + CI scaffold
- [x] Phase 1 (P3 scope) — Frontend shell, mock client, all three scenarios render
- [ ] Phase 1..2 (P1/P2 scope) — OAuth, quote signing, Decimal engine, real pillars, cache
- [x] Phase 3 (P3 scope) — Claims Portal and AG Grid ledger with live rows
- [ ] Phase 3..5 — Sandbox confirmations, calibration, hardening, Render rehearsal
