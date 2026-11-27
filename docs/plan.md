# Keitaro Campaign Manager — Implementation Plan

## 1. Purpose and delivery approach

Implement the application defined in [docs/spec.md](spec.md) as a single-backend-process web application with a React frontend, FastAPI application API, SQLAlchemy persistence, SQLite storage, and a backend-only Keitaro adapter.

Build the system in vertical, testable stages. Establish the domain rules and integration contracts before connecting the UI, then complete creation/import, local editing, and synchronization in that order. Each stage must leave the application buildable and its automated tests passing.

The specification is the source of truth. This plan describes implementation order and technical decomposition rather than introducing additional product behavior.

## 2. Initial technical choices

Unless project tooling is added later with a different explicit decision, use:

- Python with FastAPI, Pydantic settings/models, SQLAlchemy, Alembic, HTTPX, and pytest.
- React with TypeScript and Vite.
- SQLite through `DATABASE_URL`, with foreign keys and a busy timeout enabled on every connection.
- A layered backend structure:
  - API routers and request/response schemas.
  - Application services for creation, fetching, draft mutation, and upload.
  - Pure domain functions for validation, distribution, canonicalization, dirty comparison, and update planning.
  - SQLAlchemy models and repositories.
  - A Keitaro client/adapter boundary that contains all external endpoint and payload knowledge.
- Frontend API client, campaign list/create/import views, and a campaign editor route. Keep server responses authoritative; do not duplicate share or dirty-state rules in React.
- Backend unit/integration tests with a mocked Keitaro HTTP transport, plus focused frontend component tests and an end-to-end editor flow.

Keep dependency versions pinned through the selected package managers. Do not add background jobs, browser-primary persistence, automatic synchronization, or automatic conflict merging.

### New Campaign UI clarification

The creation entry point is a compact **New Campaign** dialog containing **Name**, **Geo**, and **Offer** fields plus a **Create** action. Domain, group, traffic source, and Google redirect remain backend configuration and are not form fields.

- Populate **Geo** from an API-provided list rather than a hardcoded frontend list. The backend serves a static ISO 3166-1 code/name list (Keitaro has no country-list endpoint); the initial version still submits one ISO 3166-1 alpha-2 country code, normalized to uppercase, as required by the specification.
- Implement **Offer** as autocomplete backed by the **Keitaro offer list** (`GET /offers` through the backend adapter, deleted offers excluded). Display loading, no-results, and failure states, and submit the selected Keitaro offer ID rather than free text.
- Creation must still validate that the resolved offer exists and is not deleted in Keitaro before any campaign is created.

### Application navigation and screen workflow

```text
Main screen: Campaign list
├── New Campaign → New Campaign dialog → successful creation → Edit Campaign
├── Open by Keitaro campaign ID → successful import → Edit Campaign
└── Click a campaign row → Edit Campaign
```

1. **Main screen — Campaign list**
   - Show locally known campaigns with name, Keitaro ID, dirty/synchronization state, and last synchronization time.
   - Provide **New Campaign** and **Open by Keitaro campaign ID** actions.
   - Make each campaign row navigate to that campaign’s editor route.

2. **New Campaign dialog**
   - Show **Name**, **Geo**, and **Offer** fields and a **Create** action.
   - Use the API-provided Geo list and Keitaro-backed Offer autocomplete.
   - On successful creation, close the dialog and open the new campaign’s editor with its clean baseline and persisted draft.

3. **Edit Campaign screen**
   - Load the selected campaign’s baseline metadata, editable streams/offers, pins, local revision, dirty state, notices, and synchronization state from the backend.
   - Display active and disabled streams with shares, zero-share indication, and Delete/Restore, plus expandable offer lists with Pin/Unpin controls where supported.
   - Apply all editing and share recalculation through the draft API; persist successful changes immediately and handle stale local revisions explicitly.
   - Show **Get streams from Keitaro** for a clean draft and **Upload streams to Keitaro** for a dirty draft; disable the action and show progress while an operation runs.
   - Provide confirmed **Discard changes and reload**, inline validation errors, and explicit remote-conflict and `needs_attention` recovery states.
   - Never synchronize automatically when opening the screen or in the background.

## 3. Cross-cutting invariants

Enforce these rules in backend domain code and test them independently of HTTP and React:

1. Active stream shares always total exactly 100 after every successful mutation; disabled streams always have share 0.
2. Active offer shares total exactly 100 in every offer-based stream with active offers.
3. Integer remainders are assigned in stable order: stream position then Keitaro stream ID; association ID then offer ID.
4. Pins are local-only offer metadata. Removing an offer clears its pin, re-adding starts unpinned, and pin-only changes do not affect dirty state.
5. Dirty state is derived only from canonical Keitaro-managed draft fields compared with the baseline.
6. Baseline state is stored separately from mutable draft state and is replaced only by an authorized fetch or confirmed synchronization result.
7. Every draft replacement is transactional and checks `expected_revision`; every successful mutation increments the revision.
8. Every upload re-reads the persisted draft and performs a remote fingerprint preflight before its first write.
9. Keitaro writes are partial updates containing only managed, changed fields.
10. No Keitaro credential reaches frontend assets, API responses, URLs, or logs.
11. No remote synchronization occurs implicitly or in the background.

## 4. Implementation stages

### Stage 0 — Project scaffold and Keitaro contract verification

**Work**

- Create backend and frontend applications, development commands, lint/format configuration, and test runners.
- Add environment-based backend settings for every value listed in specification section 5. Fail startup clearly when required values are missing or invalid; do not make mutating Keitaro calls at startup.
- Define the API contracts for the Geo list and offer autocomplete. The Geo list is a static backend ISO 3166-1 code/name list; offer search goes through the Keitaro adapter, so no additional upstream or credentials are needed.
- Configure restrictive CORS, structured logging, consistent API error responses, and a health endpoint that exposes no secrets.
- Against the target Keitaro version’s documentation and, with explicit operator approval, a disposable test instance, verify:
  - Campaign creation and archive payloads.
  - Stream create and partial-update payloads.
  - Exact redirect schema/action field names and Google redirect representation.
  - Offer-capable stream schema and nested association payloads.
  - Enable/disable operations and fallback partial state updates.
  - Remote campaign/stream timestamp fields and response pagination behavior.
  - Domain, group, traffic-source, and offer validation responses.
- Record verified adapter mappings in code-focused tests/fixtures. Treat undocumented or version-dependent fields as adapter capabilities, not assumptions spread through services.

**Exit criteria**

- Both applications start locally, health checks work, invalid configuration is rejected, and test commands run.
- A mocked adapter contract can represent every payload needed for creation, fetch, and upload.
- No production campaign is modified during verification.

### Stage 1 — Persistence, baseline snapshots, and synchronization metadata

**Work**

- Implement SQLAlchemy models and migrations for campaigns, draft streams, draft stream offers, immutable baseline snapshots, and synchronization metadata.
- Use local UUID keys and enforce unique Keitaro campaign IDs, stream IDs per campaign, and one local association per `(stream_id, offer_id)` so re-adding reactivates rather than duplicates.
- Store nullable Keitaro association IDs for newly added offers, display-name snapshots, positions, schemas/types, states, shares, pins, revisions, timestamps, and `needs_attention` state.
- Store a canonical baseline containing campaign/stream timestamps and all fields required for fingerprints, dirty comparison, partial updates, and compensation. Keep unsupported remote fields out of mutable update payloads.
- Add durable creation idempotency records containing the key, request hash, operation state, and original success/failure result. Ensure a failed request cannot leave a record that causes an unintended duplicate on retry.
- Add repository transactions for draft replacement, baseline replacement, revision updates, idempotency records, and synchronization-state transitions.
- Add a per-campaign upload lock. Because deployment is single-process, an async/in-process lock may coordinate active work, while persistent synchronization state must still survive failures and prevent unsafe uploads after uncertain partial synchronization.

**Tests**

- Migration and CRUD tests using SQLite with the same connection settings as production.
- Rollback tests proving failed draft replacement changes neither draft nor baseline.
- Foreign-key, uniqueness, revision, and idempotency-key/request-hash tests.
- Backend restart tests proving drafts, pins, baselines, revisions, and `needs_attention` survive.

**Exit criteria**

- Durable storage supports all later operations without browser `localStorage` as primary state.

### Stage 2 — Domain calculation and canonicalization services

**Work**

- Implement deterministic integer distribution as a shared pure function.
- Implement stream recalculation for delete and restore (equal split per specification section 6.7) and offer recalculation for add, remove, pin, and unpin using the exact `A`, `P`, `U`, and `R` rules in specification section 6.8.
- Reject atomically any mutation that leaves no unpinned active offer while pinned shares total less than 100.
- Implement offer add, remove, and reactivation with pinned-aware active shares and no effect on stream-level shares.
- Validate that an active offer-based stream has at least one active offer before restore or upload; permit an empty offer list only while that stream is disabled.
- Implement canonical Keitaro-managed projections, dirty comparison, and SHA-256 remote fingerprints. Sort keys and collections deterministically and include every field required by specification section 7.1.
- Implement baseline-versus-draft diffing to produce the minimum stream/association partial-update plan.
- Define typed validation/conflict errors that API middleware maps to the specified 400, 404, 409, 422, 502, and 504 responses.

**Tests**

- Table-driven tests for all specification examples, zero shares, disabling/restoring, invalid pin totals, deterministic rounding, and empty-stream edge cases.
- Offer rounding/reactivation tests and active-stream validation tests.
- Dirty comparison tests proving pins and UI metadata are excluded.
- Fingerprint tests proving formatting and collection order do not matter, while any managed remote change does.
- Update-plan tests proving unchanged streams and associations generate no writes.

**Exit criteria**

- All editing and synchronization calculations are correct without a running Keitaro instance or frontend.

### Stage 3 — Keitaro adapter and remote-state normalization

**Work**

- Implement the HTTPX client with `Api-Key` authentication, connection/read timeouts, response-type validation, and safe-read-only bounded retries for timeouts, 429, and transient 5xx responses.
- Implement campaign, stream, offer, domain, group, and traffic-source operations behind a typed adapter interface.
- Normalize remote campaign, stream, filter/action, timestamp, and association responses into validated internal models. Reject successful responses missing required IDs or fields.
- Preserve enough raw managed data to construct fingerprints and partial updates while never returning credentials or secret-bearing headers.
- Add sanitized operation logging with IDs, duration, operation, and status category only.
- Implement the Geo list (static ISO 3166-1 data served from the backend) and offer search over the Keitaro adapter (`GET /offers` with bounded safe-read retries and sanitized errors), translating results into stable application response models.
- Implement fetch normalization: include non-deleted active and disabled streams, omit remotely deleted streams with diagnostic notes, clear pins, zero disabled shares, and normalize invalid stream/offer totals with notices and dirty state.

**Tests**

- Mock transport coverage for success, timeout, retry exhaustion, malformed JSON/types, missing IDs, remote rejection, and pagination.
- Payload contract tests for both created streams, complementary country filters, redirect action, offer association, and partial updates.
- Geo-list and offer autocomplete tests covering result mapping, deleted-offer exclusion, empty results, malformed responses, timeouts, and upstream failures.
- Tests proving retries occur only for safe reads and never blindly for campaign creation or stream updates.

**Exit criteria**

- Application services can consume reliable internal remote models without knowing Keitaro wire details.

### Stage 4 — Campaign creation, idempotency, import, and fetch

**Work**

- Implement `POST /campaigns` with a required `Idempotency-Key`, normalized/validated name and country, positive offer ID, and a request hash.
- Before remote creation, validate configured domain/group/traffic source, offer availability, and complete local campaign/stream payloads.
- Execute creation in the required order: campaign, Google stream, offer stream, final remote fetch, baseline/draft persistence.
- On incomplete setup, stop immediately, attempt compensating campaign archive, and return the Keitaro campaign ID when known plus explicit cleanup status.
- On idempotent retry, return the original recorded result; reject reuse of the key with a different payload using 409. Coordinate concurrent duplicates so at most one creation sequence runs for a key.
- Implement campaign list and import endpoints, rejecting duplicate local imports without replacing existing state.
- Implement operator-initiated fetch with clean/dirty enforcement. Require an explicit confirmation flag to discard a dirty draft, then transactionally replace baseline and draft, clear pins, increment revision, store the fingerprint, and return normalization/deletion notices.

**Tests**

- Creation success and every failure step, including archive success/failure and responses before versus after a campaign ID exists.
- Same-key/same-payload replay, same-key/different-payload 409, and concurrent duplicate prevention with exactly one remote create.
- Verification that invalid local/configuration/offer data causes no remote writes.
- Import/fetch tests for active and disabled streams, associations, deleted remote streams, dirty-fetch 409, confirmed discard, normalization notices, and revision changes.

**Exit criteria**

- A user can create or import a campaign, reopen its persisted state, and explicitly fetch/discard without silent overwrites or duplicate campaigns.

### Stage 5 — Draft API and local editing behavior

**Work**

- Implement `GET /campaigns/{id}` returning baseline metadata, editable streams/offers, pins, revision, dirty state, notices, and upload-blocking state.
- Implement transactional `PUT /campaigns/{id}/draft` with `expected_revision` and server-side validation/recalculation.
- Define a stable draft command/payload shape for delete, restore, add offer, remove/reactivate offer, and pin/unpin offer. Validate that only supported streams expose offer editing and that names/new streams/manual percentages are not editable.
- Derive dirty state after recalculation and persist it atomically with the new revision.
- Implement `GET /offers?search=` as a case-insensitive name search over the Keitaro offer list, returning validated display data with Keitaro offer IDs. Exclude offers already actively associated with the stream being edited.
- Implement `GET /geos` from the backend's static ISO 3166-1 list, returning stable country codes and display names for the New Campaign selector. Do not expose upstream credentials or require the browser to call Keitaro directly.
- Map stale local revisions and invalid campaign rules to the specified error statuses without partial changes.

**Tests**

- Endpoint tests for every mutation, malformed IDs, stale revisions, unsupported edits, persistence rollback, and immediate reload persistence.
- Tests proving disabled rows remain, offer pins persist and clear correctly, offer reactivation does not duplicate, and pin-only changes remain clean.
- Tests proving an active offer-based stream with no active offers cannot be restored or uploaded.

**Exit criteria**

- The backend supports the complete local editor workflow with durable, revision-safe drafts.

### Stage 6 — Conflict-checked upload and partial-failure recovery

**Work**

- Implement `POST /campaigns/{id}/upload` using only the persisted draft, with no trust in a client-supplied campaign snapshot.
- Under the campaign lock, reject clean/invalid/`needs_attention` uploads as appropriate, re-read the draft, fetch current remote state, calculate its fingerprint, and compare it with the stored baseline fingerprint before any write.
- On mismatch, return 409, preserve draft and baseline, and perform no remote writes.
- Apply only planned partial updates for changed managed fields, including complete active-offer collection updates that delete removed associations and zero weights for disabled streams. Stop at the first failure.
- Track completed updates and their baseline inverse operations. After partial failure, attempt compensation in reverse dependency order and verify the result with a fresh remote read/fingerprint comparison.
- If compensation is confirmed, leave the draft dirty and report the failed operation. If compensation fails or cannot be confirmed, persist `needs_attention`, block further uploads/draft work as required, and require a fresh operator-controlled recovery fetch.
- After successful writes, fetch the resulting state, replace baseline and draft transactionally, retain offer pins only for compatible surviving active associations, and recalculate if normalization is necessary. Report clean only when the fetched managed state matches the intended values and requires no normalization.

**Tests**

- Assert zero remote writes for stale fingerprints, invalid drafts, stale local state, and clean uploads.
- Verify update order and exact partial payloads, including association additions/removals and disabled streams.
- Simulate failure at every write: successful rollback, failed rollback, unverifiable rollback, and persisted `needs_attention` after backend restart.
- Verify post-upload fetch, compatible pin retention, clean baseline, normalization notices, and no false success when returned values differ.
- Document in UI text and operational notes that fingerprint checking is optimistic and Keitaro updates are not atomic.

**Exit criteria**

- Synchronization satisfies the conflict, changed-fields-only, rollback, and recovery acceptance criteria without claiming atomicity.

### Stage 7 — React campaign workflows and editor

**Work**

- Build the campaign list with name, Keitaro ID, dirty/synchronization state, and last synchronization time.
- Add a compact **New Campaign** dialog with exactly the **Name**, **Geo**, and **Offer** inputs and a **Create** action; add a separate Open by Keitaro campaign ID form for import.
- Load Geo choices from `GET /geos` into a single-select field with loading, empty, and error states. Submit one normalized ISO alpha-2 code rather than display text.
- Implement Offer autocomplete using `GET /offers?search=` and the Keitaro-backed integration. Show loading, no-results, and request-failure states; require a selected offer and submit its Keitaro offer ID rather than arbitrary text.
- Apply client-side usability validation while retaining backend authority for all fields and upstream-offer validation.
- Build the editor route and primary synchronization button:
  - Clean: **Get streams from Keitaro**.
  - Dirty: **Upload streams to Keitaro**.
  - In progress: disabled with a loading indicator.
- Render streams sorted by position then ID, including visually distinct disabled rows, read-only names, integer shares, zero-share indication, and Delete/Restore actions.
- Add expandable offer lists only for offer-based streams, with pin controls, searchable existing-offer selection, and no global offer editing.
- Send draft mutations with the latest revision and use returned state as authoritative. Optimistic updates are optional; if used, roll them back on failure.
- Show field/stream/offer validation errors, operation failures, normalization notices, and remote conflicts without exposing raw external responses.
- Require confirmation for **Discard changes and reload** and for recovery that intentionally replaces local edits. Disable upload when validation or `needs_attention` blocks it.
- Handle stale-revision errors by asking the operator to reload the latest local draft rather than silently retrying over another tab’s edits.

**Tests**

- Component tests for the New Campaign dialog, API-loaded Geo options, Keitaro-backed Offer autocomplete states, selected-ID submission, and Create validation.
- Component tests for primary-button states, disabled rows, zero-share/pin controls, offer selector exclusions, confirmations, and inline errors.
- API-client tests for idempotency and revision headers/payloads.
- End-to-end browser tests against the real backend with mocked Keitaro for create/import, edit persistence across reload, dirty fetch conflict, offer editing, upload conflict, and successful upload.

**Exit criteria**

- All specified workflows are usable through React, and no Keitaro request or credential is present in the browser.

### Stage 8 — Security, operational readiness, and acceptance verification

**Work**

- Add structured error categories for validation, local conflict, remote conflict, timeout, remote rejection, and partial synchronization.
- Audit logs, errors, frontend bundles, network calls, and API responses for credential leakage.
- Confirm SQLite foreign keys/busy timeout, single-process deployment instructions, restricted CORS, database backup considerations, and explicit HTTPS/authentication requirements for untrusted networks.
- Document local setup, environment variables without secrets, migration/test/build commands, recovery from `needs_attention`, and the non-atomic limits of Keitaro synchronization.
- Run the complete backend/frontend test suites, production builds, and browser end-to-end flows.
- Map each of the 22 acceptance criteria in specification section 12 to an automated test or a documented manual verification step.

**Manual Keitaro verification — explicit operator approval required**

Use only a dedicated disposable Keitaro test campaign/instance:

1. Create a campaign and verify its configured domain/group/source, weighted rotation, stream order, names, initial shares, filters, redirect, and offer association directly in Keitaro.
2. Verify selected-country traffic reaches Google and all other traffic reaches the offer; verify weighted offer distribution where applicable.
3. Change remote state externally and confirm upload returns 409 before any write.
4. Edit locally, upload, and verify only intended remote-managed fields changed.
5. Exercise timeout/partial-failure recovery only in a controlled test environment.

**Exit criteria**

- All automated checks and production builds pass.
- All acceptance criteria have evidence; any unperformed live Keitaro checks are reported explicitly rather than assumed.

## 5. Suggested implementation sequence and review gates

1. Approve Stage 0 adapter contracts before building creation payloads.
2. Complete and review Stages 1–2 before external writes; these establish data safety and core invariants.
3. Demonstrate Stage 4 creation/import with mocked Keitaro before any disposable live test.
4. Complete Stage 5 local editing before implementing upload, so synchronization always operates on proven persisted drafts.
5. Treat Stage 6 failure-path tests as a release gate, not optional hardening.
6. Integrate Stage 7 against the real application API rather than frontend mocks alone.
7. Finish Stage 8 only after the complete acceptance matrix passes or remaining live checks are explicitly approved/deferred by the operator.

## 6. Out of scope for this implementation

Do not add offer creation/global deletion, stream creation or renaming in the editor, manual percentage entry, unrelated campaign settings, permanent deletion, automatic merge, background synchronization, analytics, or multi-user roles. Network authentication and HTTPS remain deployment prerequisites when the application is exposed beyond a trusted single-operator environment, not substitutes for the application’s validation and conflict controls.
