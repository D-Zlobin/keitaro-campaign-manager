# Keitaro Campaign Manager

Keitaro Campaign Manager is a web application for creating and editing Keitaro
campaigns. A **Campaign** contains **Streams**, which route traffic; offer-based
streams reference **Offers**. New Campaign creates a Google stream for the
selected Geo and an offer stream for the remaining traffic, using the configured
domain, group, and traffic source. Existing campaigns can be imported, edited
locally, and explicitly synchronized with Keitaro.

## Setup and startup

Requires Python 3.12+ and Node.js 20+.

### 1. Configure and start the backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

Set `KEITARO_API_KEY` in `backend/.env`. The example configuration supplies the
other development defaults, including the test Keitaro instance and SQLite
database.

Apply the database migrations and start the API on port 8000:

```bash
.venv/bin/alembic upgrade head
.venv/bin/uvicorn --factory app.main:create_app --reload
```

### 2. Start the frontend

In a second terminal, from the repository root:

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api` requests to the backend on
port 8000. The health endpoint is `GET /api/v1/health` and returns
`{"status": "ok"}` without exposing secrets.

## Project layout

- `backend/` — FastAPI application (`app/`), tests (`tests/`), and read-only
  verification scripts (`scripts/`). All Keitaro knowledge lives in
  `backend/app/keitaro/`; the browser never talks to Keitaro.
- `frontend/` — React + TypeScript (Vite) campaign list, creation dialog,
  import, and stream/offer editor. Component tests run against a mocked
  application API; browser end-to-end tests run against the real backend
  with a fake Keitaro upstream.
- `docs/` — product specifications and implementation plans.

## Backend

Configuration comes from the environment, then `backend/.env` — the only
dotenv file, anchored to the backend directory so the launch directory does
not matter. Startup fails with a clear, secret-free error when a required
value is missing or invalid (`app/config.py`).

Errors use the envelope `{"error": {"code", "message", "details"}}`.
CORS allows only `CORS_ALLOWED_ORIGINS` (default `http://localhost:5173`).

Backend checks:

```bash
cd backend
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

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
  backend validates the configured domain/group/traffic source and offer
  availability; on incomplete setup it stops and attempts a compensating
  campaign archive, reporting `keitaro_campaign_id` and `cleanup_status` in
  the error details.
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
  share while the remaining active offers split the rest. The backend
  recalculates shares, validates campaign rules (422), derives dirty state
  from the canonical baseline (pin-only changes stay clean), and persists
  atomically; a stale `expected_revision` is a 409, unknown draft IDs are 404. New offers are re-validated against Keitaro before they persist.
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
(`app/db/engine.py`). Migrations use Alembic and are applied during setup.
To roll back all migrations:

```bash
cd backend
.venv/bin/alembic downgrade base
```

Tests migrate a per-test SQLite file through the same Alembic environment,
then connect with the production engine builder.

## Frontend

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
  conflict/stale-revision/hard-reset recovery. Every draft mutation sends
  `expected_revision`; server responses are authoritative and
  synchronization is never implicit.
- Routing is hash-based (`#/`, `#/campaigns/{id}`), so the production build
  works from any static file server.

Frontend checks:

```bash
cd frontend
npm run test
npm run lint
npm run format:check
npm run build
```

### End-to-end browser tests

`frontend/e2e/` runs Playwright (Chromium) against the **real backend** —
FastAPI, SQLite, services, and revision/lock handling — with only Keitaro
replaced by an in-memory fake. The Playwright `webServer` builds the frontend
and starts `backend/scripts/e2e_server.py`, which serves the built app and the
API on one origin and exposes `/e2e/*` control endpoints (state reset,
simulated remote drift) that exist only in this harness. Covered flows:
creation, import (including duplicate rejection), edit persistence across
reload, dirty-fetch conflict and confirmed discard, offer
add/remove/reactivation, upload conflict with zero remote writes, and
successful upload verified against the fake remote.

```bash
cd frontend
npm run test:e2e
```

## Keitaro contract verification

The adapter contracts were checked against the [official OpenAPI
reference](https://admin-api.docs.keitaro.io/) and, with operator approval,
read-only `GET` calls to the disposable test instance on 2026-11-25. Mappings
are recorded in `backend/app/keitaro/payloads.py`, `models.py`, and
`backend/tests/keitaro/fixtures.py`. Re-run the read-only check with:

```bash
cd backend
.venv/bin/python scripts/verify_keitaro_readonly.py
```

Confirmed on the target instance:

- Auth via `Api-Key` header; lists return plain JSON arrays.
- Campaign create requires a non-empty `alias` and `name`; rotation supports
  `type: "weight"`; archive is `DELETE /campaigns/{id}`; campaigns carry
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

Remaining verification:

- **Redirect representation** (`action_payload` URL string vs
  `action_options`) needs one write test on a disposable campaign, or an
  operator check of the admin UI. Both variants are implemented
  (`redirect_via` in `payloads.py`).
