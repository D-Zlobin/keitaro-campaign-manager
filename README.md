# Keitaro Campaign Manager

Web application that creates Keitaro campaigns with a predefined two-stream
structure and edits streams and offer associations of existing campaigns.
See `docs/spec.md` (source of truth) and `docs/plan.md` (implementation
stages). Current status: **stages 0–4 complete** (scaffold and Keitaro
contract verification; persistence, baselines, and synchronization
metadata; domain share calculation, canonicalization, and update planning;
Keitaro adapter with bounded retries, pagination, and fetch normalization,
plus the Five API Geo/offer adapters; campaign creation with idempotency
and compensating cleanup, import, and operator-initiated fetch).

## Layout

- `backend/` — FastAPI application (`app/`), tests (`tests/`), read-only
  verification script (`scripts/`). All Keitaro knowledge lives in
  `backend/app/keitaro/`; the browser never talks to Keitaro.
- `frontend/` — React + TypeScript (Vite). UI workflows arrive in stage 7.
- `docs/` — specification and plan.

## Backend

Requires Python 3.12+.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # pinned; includes runtime deps
cp .env.example .env                            # dev defaults for the test instance
# put KEITARO_API_KEY into the repository-root .env (or backend/.env)
```

Configuration comes from the environment, then `backend/.env`, then the
root `.env` (highest precedence). Startup fails with a clear, secret-free
error when a required value is missing or invalid (`app/config.py`).

```bash
.venv/bin/uvicorn --factory app.main:create_app --reload   # serves :8000
.venv/bin/python -m pytest                                 # tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .  # lint / format
```

Health check: `GET /api/v1/health` → `{"status": "ok"}` (no secrets).
Errors use the envelope `{"error": {"code", "message", "details"}}`.
CORS allows only `CORS_ALLOWED_ORIGINS` (default `http://localhost:5173`).

### Application API (stage 5)

- `GET /api/v1/campaigns` — list locally known campaigns (name, Keitaro ID,
  sync state, revision, last fetch/upload times).
- `POST /api/v1/campaigns` — create a campaign remotely and locally.
  Requires an `Idempotency-Key` header: replaying the same key returns the
  recorded result (success or failure) without new remote work; reusing a
  key with a different payload is a 409. Body: `{name, country_code,
  offer_id}` (name trimmed, country normalized to uppercase ISO 3166-1
  alpha-2). Before any write the backend validates the configured
  domain/group/traffic source and offer availability; on incomplete setup
  it stops and attempts a compensating campaign archive, reporting
  `keitaro_campaign_id` and `cleanup_status` in the error details.
- `POST /api/v1/campaigns/import` — import by `{keitaro_campaign_id}`;
  duplicate imports are a 409 and never replace existing state.
- `POST /api/v1/campaigns/{id}/fetch` — operator-initiated fetch replacing
  baseline and draft (pins cleared, revision incremented, normalization
  notices returned). A dirty or `needs_attention` draft requires
  `{"confirm_discard": true}`; without it the fetch is a 409.
- `GET /api/v1/campaigns/{id}` — editor load state: baseline metadata,
  editable streams/offers (with pins and `offer_capable`), revision, dirty
  state, persisted notices, and `upload_blocked`.
- `PUT /api/v1/campaigns/{id}/draft` — one transactional draft mutation:
  `{expected_revision, command}`. Commands are `delete_stream`,
  `restore_stream`, `pin_stream`, `unpin_stream`, `add_offer`, and
  `remove_offer` (with `stream_id`/`offer_id`); percentages, renames, and
  new streams are not expressible. The backend recalculates shares,
  validates campaign rules (422), derives dirty state from the canonical
  baseline (pin-only changes stay clean), and persists atomically; a stale
  `expected_revision` is a 409, unknown draft IDs are 404. New offers are
  re-validated against Keitaro before they persist.
- `GET /api/v1/geos` — country codes and display names for the New
  Campaign dialog, proxied from the server-side Five API integration
  (503 while unconfigured).
- `GET /api/v1/offers?search=` — offer autocomplete returning resolved
  Keitaro offer IDs. With `campaign_id` and `stream_id` (both required
  together) offers already active in that stream are excluded; disabled
  associations stay selectable for reactivation.

Upload arrives in stage 6.

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
npm run test           # vitest
npm run lint           # oxlint
npm run format:check   # prettier
npm run build          # production build
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
- **Five API** (Geo list + offer autocomplete upstream): implemented in
  `backend/app/five/` against an assumed contract (bearer auth,
  `GET /geos` → `[{code, name}]`, `GET /offers?search=` →
  `[{keitaro_offer_id, name}]`; see the package docstring). Auth shape,
  search parameters, and the Keitaro offer-ID mapping remain unverified
  pending Five API docs/credentials — adjust `app/five/models.py` when
  confirmed. Credentials are server-only settings and never reach the
  browser; adapters retry safe reads only and redact the key from errors
  and logs.
