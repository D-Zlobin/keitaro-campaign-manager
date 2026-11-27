# Keitaro Campaign Manager

Web application that creates Keitaro campaigns with a predefined two-stream
structure and edits streams and offer associations of existing campaigns.
See `docs/spec.md` (source of truth) and `docs/plan.md` (implementation
stages). Current status: **stages 0–7 complete** (scaffold and Keitaro
contract verification; persistence, baselines, and synchronization
metadata; domain share calculation, canonicalization, and update planning;
Keitaro adapter with bounded retries, pagination, and fetch normalization,
plus the Keitaro-backed Geo/offer catalogs; campaign creation with idempotency
and compensating cleanup, import, and operator-initiated fetch; the
revision-safe draft editing API; conflict-checked upload with rollback and
`needs_attention` recovery; the React campaign workflows and editor).

## Layout

- `backend/` — FastAPI application (`app/`), tests (`tests/`), read-only
  verification script (`scripts/`). All Keitaro knowledge lives in
  `backend/app/keitaro/`; the browser never talks to Keitaro.
- `frontend/` — React + TypeScript (Vite) campaign list, creation dialog,
  import, and stream/offer editor. Component tests run against a mocked
  application API; browser end-to-end tests run against the real backend
  with a fake Keitaro upstream.
- `docs/` — specification and plan.

## Backend

Requires Python 3.12+.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # pinned; includes runtime deps
cp .env.example .env                            # dev defaults for the test instance
# put KEITARO_API_KEY into backend/.env
```

Configuration comes from the environment, then `backend/.env` — the only
dotenv file, anchored to the backend directory so the launch directory does
not matter. Startup fails with a clear, secret-free error when a required
value is missing or invalid (`app/config.py`).

```bash
.venv/bin/uvicorn --factory app.main:create_app --reload   # serves :8000
.venv/bin/python -m pytest                                 # tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .  # lint / format
```

Health check: `GET /api/v1/health` → `{"status": "ok"}` (no secrets).
Errors use the envelope `{"error": {"code", "message", "details"}}`.
CORS allows only `CORS_ALLOWED_ORIGINS` (default `http://localhost:5173`).

### Application API

- `GET /api/v1/campaigns` — list locally known campaigns (name, Keitaro ID,
  sync state, revision, last fetch/upload times).
- `POST /api/v1/campaigns` — create a campaign remotely and locally.
  Requires an `Idempotency-Key` header: replaying the same key returns the
  recorded result (success or failure) without new remote work; reusing a
  key with a different payload is a 409. Body: `{name, country_code,
  offer_id}` (name trimmed, country normalized to uppercase ISO 3166-1
  alpha-2). The backend generates a unique non-empty campaign alias because
  the target Keitaro instance rejects empty aliases. Before any write the
  backend validates the configured
  domain/group/traffic source and offer availability; on incomplete setup
  it stops and attempts a compensating campaign archive, reporting
  `keitaro_campaign_id` and `cleanup_status` in the error details.
- `POST /api/v1/campaigns/import` — import by `{keitaro_campaign_id}`;
  duplicate imports are a 409 and never replace existing state.
- `POST /api/v1/campaigns/{id}/fetch` — operator-initiated fetch replacing
  baseline and draft (pins cleared, revision incremented, normalization
  notices returned). A dirty, `syncing`, or `needs_attention` draft requires
  `{"confirm_discard": true}`; without it the fetch is a 409. Confirmed
  recovery from `syncing` is serialized with uploads by the campaign lock.
- `GET /api/v1/campaigns/{id}` — editor load state: baseline metadata,
  editable streams/offers (with pins and `offer_capable`), revision, dirty
  state, persisted notices, and `upload_blocked`.
- `PUT /api/v1/campaigns/{id}/draft` — one transactional draft mutation:
  `{expected_revision, command}`. Commands are `delete_stream`,
  `restore_stream`, `add_offer`, `remove_offer`, `pin_offer`, and
  `unpin_offer` (with `stream_id`/`offer_id`); percentages, renames, and
  new streams are not expressible. Stream shares always redistribute
  equally; pins are local offer-level metadata — a pinned offer keeps its
  share while the remaining active offers split the rest. The backend recalculates shares,
  validates campaign rules (422), derives dirty state from the canonical
  baseline (pin-only changes stay clean), and persists atomically; a stale
  `expected_revision` is a 409, unknown draft IDs are 404. New offers are
  re-validated against Keitaro before they persist.
- `GET /api/v1/geos` — country codes and display names for the New
  Campaign dialog, served from the backend's static ISO 3166-1 list
  (Keitaro has no country-list endpoint).
- `GET /api/v1/offers?search=` — offer autocomplete: a case-insensitive
  name search over Keitaro's offers (`GET /offers`, deleted offers
  excluded), returning Keitaro offer IDs. With `campaign_id` and
  `stream_id` (both required together) offers already active in that
  stream are excluded; disabled associations stay selectable for
  reactivation.

- `POST /api/v1/campaigns/{id}/upload` — push the persisted dirty draft to
  Keitaro (no request body; the client never supplies a campaign snapshot).
  Before any write the backend re-reads the draft, validates it, and
  compares a fingerprint of the current remote state with the stored
  baseline: a mismatch is a 409 `remote_conflict` with zero remote writes.
  Only changed managed fields are sent as per-stream partial updates. Offer
  updates send the complete active association set: removed offers are deleted
  from the Keitaro stream but remain disabled locally without a remote ID.
  - A clean draft is a 409 `clean_draft`; `syncing`/`needs_attention`
    campaigns are 409; an invalid persisted draft is a 422 `invalid_draft`.
  - On a write failure the attempted updates are compensated in reverse
    order and the rollback is verified with a fresh read: confirmed
    rollback returns 502 `upload_failed` and keeps the draft dirty;
    unconfirmed rollback persists `needs_attention` (surviving restarts),
    blocks uploads and draft edits, and requires a confirmed recovery
    fetch. A crash mid-upload leaves `syncing`, which startup resets to
    `dirty`; the next preflight detects any partial writes.
  - After successful writes the backend fetches the result and replaces
    baseline and draft; the response is clean only when the fetched state
    matches the uploaded values exactly. Normalization or divergence keeps
    the draft dirty with explanatory `notices` — never a false success.

**Synchronization is not atomic.** The fingerprint preflight is optimistic
conflict detection: Keitaro has no compare-and-set, so a remote write
landing between the preflight and our updates cannot be detected, and a
multi-stream upload can partially apply (the rollback above is the
mitigation). Do not present synchronization as transactional in the UI.

### Database

SQLite via `DATABASE_URL`. SQLAlchemy models live in `app/db/models.py`;
repositories (draft/baseline/idempotency/sync-state) in `app/repositories/`.
Every connection enables foreign keys and a busy timeout
(`app/db/engine.py`). Migrations are Alembic:

```bash
.venv/bin/alembic upgrade head     # apply migrations (URL from DATABASE_URL)
.venv/bin/alembic downgrade base   # roll back
```

Tests migrate a per-test SQLite file through the same Alembic environment,
then connect with the production engine builder.

## Frontend

Requires Node 20+.

```bash
cd frontend
npm install
npm run dev            # serves :5173, proxies /api to :8000
npm run test           # vitest component and API-client tests
npm run test:e2e       # Playwright browser tests (see below)
npm run lint           # oxlint
npm run format:check   # prettier
npm run build          # production build
```

Structure (`frontend/src/`):

- `api/` — typed client for the application API. Parses the error envelope
  into `ApiError` codes (`stale_revision`, `remote_conflict`, ...) and sends
  the `Idempotency-Key` header on creation; the browser never sees upstream
  credentials or raw upstream responses.
- `pages/CampaignListPage.tsx` — main screen: campaign list (name, Keitaro
  ID, sync state, last synchronization), the compact **New Campaign** dialog
  (Name, API-loaded Geo select, Keitaro-backed Offer autocomplete that
  submits the resolved Keitaro offer ID), and the Open-by-Keitaro-ID import
  form.
- `pages/CampaignEditorPage.tsx` — editor: primary button (**Get streams
  from Keitaro** when clean, **Upload streams to Keitaro** when dirty,
  disabled with progress while running), streams table with Delete/Restore,
  disabled rows, default-expanded offer lists with Pin/Unpin controls and
  exclusion-scoped offer search, normalization notices, and explicit
  conflict/stale-revision/hard-reset recovery. Every draft
  mutation sends `expected_revision`; server responses are authoritative and
  synchronization is never implicit.
- Routing is hash-based (`#/`, `#/campaigns/{id}`), so the production build
  works from any static file server.

### End-to-end browser tests

`frontend/e2e/` runs Playwright (Chromium) against the **real backend** —
FastAPI, SQLite, services, and revision/lock handling — with only Keitaro
and the Keitaro API replaced by an in-memory fake. The Playwright `webServer`
builds the frontend and starts `backend/scripts/e2e_server.py`, which
serves the built app and the API on one origin and exposes `/e2e/*` control
endpoints (state reset, simulated remote drift) that exist only in this
harness. Covered flows: creation, import (including duplicate rejection),
edit persistence across reload, dirty-fetch conflict and confirmed discard,
offer add/remove/reactivation, upload conflict with zero remote writes, and
successful upload verified against the fake remote.

```bash
cd frontend
npm run test:e2e       # builds frontend, starts the harness, runs Chromium
```

## Stage 0 Keitaro contract verification

Verified against the official OpenAPI reference
(admin-api.docs.keitaro.io) and — with operator approval — read-only `GET`
calls to the disposable test instance on 2026-11-25. Mappings are recorded
in `backend/app/keitaro/payloads.py` (docstring), `models.py`, and
`backend/tests/keitaro/fixtures.py`; re-run the read-only check with:

```bash
cd backend && .venv/bin/python scripts/verify_keitaro_readonly.py
```

Confirmed on the target instance:

- Auth via `Api-Key` header; lists return plain JSON arrays.
- Campaign create requires `alias` + `name`; rotation `type: "weight"`;
  archive is `DELETE /campaigns/{id}`; campaigns carry
  `created_at`/`updated_at` and support `limit`/`offset`.
- Streams: create requires `campaign_id`, `schema`, `type`, `name`,
  `action_type`; offer-capable schema is `landings` with nested
  `offers: [{offer_id, share, state}]` (integer shares); UI-created
  landings streams carry `action_type: "http"`; stream `updated_at` may be
  `null`; `/streams/{id}/disable|enable` exist; `PUT /streams/{id}` updates
  only provided fields.
- Offer `country` values are uppercase ISO 3166-1 alpha-2 codes.
- The instance has no `/streams_actions`, `/stream_schemas`,
  `/stream_filters`, or `/version` endpoints — action/schema/filter names
  are adapter constants, not discoverable metadata.

Open items (not blockers for stages 4–6):

- **Redirect representation** (`action_payload` URL string vs
  `action_options`) needs one write test on a disposable campaign, or an
  operator check of the admin UI, before the first live creation. Both
  variants are implemented (`redirect_via` in `payloads.py`).
