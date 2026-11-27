# New Campaign Seed Alignment Plan

## 1. Goal and scope

Align the existing **New Campaign** dialog with [docs/create-campaign-seed.md](create-campaign-seed.md) while leaving its visual design unchanged.

A successful submission must accept a campaign name, country Geo code, and Keitaro offer ID, then create a campaign with:

- Domain `kt-emulator.test`, group `FORTESTS`, and traffic source `FORTESTS`, resolved through backend configuration and the Keitaro API.
- A first stream that accepts the selected country and redirects to Google.
- A second stream that sends the remaining traffic to the selected offer.
- Exactly these two streams, created in that order.

Do not add domain, group, source, redirect URL, stream, or weight inputs to the dialog. Campaign import and the campaign editor are out of scope except for verifying the existing post-creation navigation.

## 2. Current-state findings

Static inspection shows that the requested behavior is already largely implemented:

- `frontend/src/components/NewCampaignDialog.tsx` preserves the compact Name / Geo / Offer form and submits `{name, country_code, offer_id}` with an idempotency key.
- `backend/app/api/schemas.py` normalizes and validates the creation request.
- `backend/app/services/campaigns.py` validates remote configuration and offer availability before writing, then creates the campaign, Google stream, and offer stream in the required order.
- `backend/app/keitaro/payloads.py` builds the configured campaign payload and two complementary Geo-filtered stream payloads.
- `backend/.env.example` already points to the test Keitaro instance, configures IDs for `kt-emulator.test` / `FORTESTS` / `FORTESTS`, and uses `https://www.google.com/`.
- Backend unit/API tests and frontend component/browser tests already cover the main creation flow and exact fake-Keitaro payloads.

Therefore, do not rewrite working creation code or redesign the dialog. The work is a focused contract audit, correction of any demonstrated mismatch, and end-to-end verification. The known remaining technical risk is the live Keitaro representation of the Google redirect: `action_payload` is implemented, while `action_options` remains a supported alternative pending a verified write on the disposable test instance.

## 3. Implementation steps

### Step 1 — Preserve and verify the dialog contract

- Keep the existing dialog structure, styles, labels, and actions unchanged.
- Confirm that Name is trimmed and required, Geo submits one uppercase ISO 3166-1 alpha-2 code, and Offer submits the selected positive Keitaro offer ID rather than display text.
- Keep client validation, loading/error states, and the stable per-dialog idempotency key. A failed submission must leave the dialog open; a retry must not create a duplicate campaign.
- On success, retain the existing behavior: close the dialog and open the created campaign editor with a clean persisted baseline.

Primary files: `frontend/src/components/NewCampaignDialog.tsx`, `frontend/src/api/campaigns.ts`, `frontend/src/api/types.ts`.

### Step 2 — Verify backend input and pre-write validation

- Use the existing `POST /api/v1/campaigns` endpoint; no new browser-to-Keitaro integration or public API shape is needed.
- Confirm malformed input is rejected before remote work.
- Confirm the configured domain, group, and traffic source exist and are usable, and that the selected offer exists and is not deleted, before campaign creation.
- Resolve the seed names to their configured IDs on the target instance. Keep IDs server-side in environment settings; do not hardcode them in React or expose the Keitaro API key.

Primary files: `backend/app/api/campaigns.py`, `backend/app/api/schemas.py`, `backend/app/services/campaigns.py`, `backend/app/config.py`, `backend/.env.example`.

### Step 3 — Verify the exact Keitaro creation payloads

Confirm the campaign payload contains the supplied name and configured `domain_id`, `group_id`, and `traffic_source_id`.

Confirm exactly two active streams are sent in this order:

1. **Google — `{GEO}`**: position 0, redirect schema/action, `https://www.google.com/`, country filter `accept: [GEO]`, and no offers.
2. **Offer — `{OFFER_ID}`**: position 1, offer-capable `landings` schema, country filter `reject: [GEO]`, and one active association for the selected offer at 100% within the stream.

Retain the existing 50/50 stream weights required by the broader specification; the complementary Geo filters, not those weights, determine country routing. Keep Keitaro wire-format details inside `backend/app/keitaro/`.

If live verification proves that the target Keitaro version requires `action_options` instead of `action_payload`, change only the adapter mapping and its contract fixtures/tests. Do not spread version-specific handling into the service or UI.

Primary files: `backend/app/keitaro/payloads.py`, `backend/app/keitaro/adapter.py`, `backend/app/keitaro/client.py`, `backend/tests/keitaro/test_payloads.py`.

### Step 4 — Verify orchestration, persistence, and failure handling

- Preserve the write order: campaign, Google stream, offer stream, final remote fetch, transactional local baseline/draft persistence.
- On a failed step, stop immediately. If a Keitaro campaign ID is known, retain the existing compensating archive attempt and report the campaign ID and cleanup outcome.
- Preserve same-key replay and same-key/different-payload conflict behavior so retries cannot silently duplicate campaigns.
- Return a successfully created campaign as clean only after its fetched remote state has been persisted.

No database migration is expected for this seed alignment; add one only if a concrete, newly demonstrated persistence requirement appears.

Primary files: `backend/app/services/campaigns.py`, `backend/tests/services/test_campaign_creation.py`, `backend/tests/api/test_campaigns.py`.

### Step 5 — Strengthen only missing test evidence

Reuse the existing tests rather than creating parallel test scaffolding:

- `frontend/src/components/NewCampaignDialog.test.tsx`: field validation, selected-ID/Geo submission, idempotent retry, error display, and successful callback.
- `backend/tests/keitaro/test_payloads.py`: exact campaign, Google redirect, complementary filters, stream order inputs, and offer association payloads.
- `backend/tests/services/test_campaign_creation.py`: validation before writes, exact remote call order, cleanup, idempotency, and persisted clean state.
- `backend/tests/api/test_campaigns.py`: request normalization and HTTP error/success contracts.
- `frontend/e2e/create.spec.ts`: real browser → backend → fake Keitaro creation flow and editor navigation.

Add assertions only where a seed requirement lacks coverage. In particular, ensure the end-to-end creation test can verify the fake remote campaign’s configured domain/group/source and exact two-stream result, not only the editor labels.

## 4. Verification

Run the project’s standard checks on the final implementation:

```bash
cd backend
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .

cd ../frontend
npm run test
npm run lint
npm run format:check
npm run build
npm run test:e2e
```

After automated checks pass, perform one controlled live creation on the disposable Keitaro test instance **only with explicit operator approval**. Verify directly in Keitaro that:

- The campaign uses `kt-emulator.test`, group `FORTESTS`, and source `FORTESTS`.
- There are exactly two streams in the required order.
- Selected-Geo traffic matches only the Google stream and reaches `https://www.google.com/`.
- Other traffic matches only the offer stream and reaches the selected offer.
- The application reopens the created campaign with the same clean two-stream state.

If live write verification is not approved or cannot be completed, report it as pending; do not infer success from mocked tests alone.

## 5. Acceptance criteria

- The New Campaign visual design and three-field workflow remain unchanged.
- A valid submission creates one Keitaro campaign with the required configured domain, group, and source.
- The campaign contains exactly the required Google and offer streams, with correct order, Geo routing, redirect target, and selected offer.
- Invalid input or invalid remote configuration causes no campaign writes.
- Partial creation failures stop the sequence and report compensating cleanup status; retries do not create duplicates.
- Success opens a clean persisted campaign editor; failure keeps the dialog open with an actionable error.
- All backend/frontend tests, lint/format checks, frontend build, and browser creation flow pass.
- Any unperformed live Keitaro verification is explicitly identified rather than presented as complete.

## 6. Assumptions and boundaries

- `https://www.google.com/` is the concrete Google redirect target for `google.com` in the seed.
- The existing Geo selector and offer autocomplete satisfy the seed’s Geo-code and offer-ID inputs while providing safer selection.
- The second stream’s `reject` Geo filter is intentional: it makes the offer stream handle all traffic not accepted by the first stream.
- Existing domain/group/source IDs in `backend/.env.example` are defaults for the disposable test instance; deployment-specific values remain environment configuration.
- No live mutating Keitaro call is part of implementation without explicit operator approval.
