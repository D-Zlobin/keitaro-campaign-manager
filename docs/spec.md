# Keitaro Campaign Manager — Product and Technical Specification

## 1. Purpose

Build a small web application that creates Keitaro campaigns with a predefined two-stream structure and edits the streams and offer associations of existing campaigns.

The application must provide a persistent local draft, make unsynchronized changes visible, prevent accidental overwrite of changed remote data, and keep traffic-distribution percentages valid.

This document supersedes the behavioral gaps in `docs/spec-seed.md`. The seed remains the original input, not an additional source of conflicting requirements.

## 2. Terminology

- **Keitaro**: the external advertising tracker and source of truth for live campaign configuration.
- **Campaign**: a Keitaro campaign managed by this application.
- **Stream**: a Keitaro flow. The UI uses “Stream”; the Keitaro API uses “flow” and the `/streams` endpoints.
- **Offer**: an existing Keitaro offer. The application does not create or globally delete offers.
- **Offer association**: an offer attached to a stream, with its own state and `share`.
- **Stream share**: the UI percentage mapped to Keitaro stream `weight`. Active stream shares total 100%.
- **Offer share**: the percentage mapped to an offer association’s `share`. Active offer shares total 100% within each offer-based stream.
- **Pinned offer**: an active offer association whose percentage is excluded from automatic redistribution within its stream. Pinning is local application metadata, not a Keitaro feature.
- **Baseline**: the last campaign and stream state successfully fetched from Keitaro.
- **Draft**: the persistent local editable state.
- **Dirty**: the draft’s Keitaro-managed fields differ from the baseline.
- **Disabled stream**: a soft-deleted draft stream. Its row remains visible locally and can be restored.

## 3. Goals and non-goals

### 3.1 Goals

1. Create a campaign from a name, ISO country code, and existing Keitaro offer ID.
2. Configure the campaign’s domain, group, traffic source, and Google redirect from backend settings.
3. Import an existing Keitaro campaign by ID.
4. Display active and disabled streams and their offer associations.
5. Add and remove offers per stream with equal offer shares.
6. Soft-delete and restore streams.
7. Automatically redistribute stream shares equally, and offer shares while honoring pinned offers.
8. Persist drafts across browser reloads and application restarts.
9. Explicitly pull from and push to Keitaro; never synchronize silently in the background.
10. Detect stale remote state before pushing and reject conflicting writes.

### 3.2 Non-goals for the initial version

- Creating or editing Keitaro offers themselves.
- Creating streams in the editor, renaming streams, or manually entering stream percentages.
- Editing landing pages, filters other than those created for a new campaign, triggers, postbacks, costs, or campaign analytics.
- Deleting a campaign or permanently deleting a stream.
- Merging concurrent local and remote edits automatically.
- Background synchronization or scheduled jobs.
- Multi-user roles and permissions. The initial version assumes one trusted operator; see Security.

## 4. Technology and architecture

- **Backend**: Python, FastAPI, SQLAlchemy, and an HTTP client for Keitaro.
- **Frontend**: React.
- **Persistence**: a server-side relational database accessed through SQLAlchemy.
- **Integration**: all Keitaro calls originate in the backend. The browser never receives the Keitaro API key and never calls Keitaro directly.
- **Application structure**:
  - React UI for campaign creation and editing.
  - FastAPI application API for validation, draft mutations, percentage calculation, and synchronization.
  - A Keitaro adapter isolating external endpoint and payload details.
  - SQLAlchemy repositories for campaigns, drafts, baselines, and synchronization metadata.

### 4.1 Database decision

The project uses **SQLite** as its persistent relational database for development, testing, and the initial deployment. SQLAlchemy is only an abstraction layer and does not replace the database.

- Store the SQLite database in a backend-managed file configured through `DATABASE_URL`.
- Run the application as a single backend process to avoid unsupported concurrent-writer deployment patterns.
- Enable SQLite foreign-key enforcement on every connection and configure a busy timeout for short lock contention.
- Use database transactions for draft replacement, synchronization metadata updates, and idempotency records.
- Do not use browser `localStorage` as the primary store. It cannot reliably provide shared backend state, conflict detection, durable baselines, or recovery after partial synchronization.

The database belongs to the backend; “local database” does not mean a database embedded in the browser. A future move to multiple backend workers or a different database engine requires a separate design decision and migration plan.

## 5. Configuration

Backend configuration must include:

| Setting | Required | Description |
|---|---:|---|
| `KEITARO_BASE_URL` | Yes | Base URL ending in `/admin_api/v1`. |
| `KEITARO_API_KEY` | Yes | Server-only Keitaro Admin API credential. |
| `KEITARO_DOMAIN_ID` | Yes | Existing active Keitaro domain ID used for new campaigns. |
| `KEITARO_GROUP_ID` | Yes | Existing campaign group ID used for new campaigns. |
| `KEITARO_TRAFFIC_SOURCE_ID` | Yes | Existing traffic source ID used for new campaigns. |
| `GOOGLE_REDIRECT_URL` | Yes | Absolute HTTPS URL used by the Google stream; normally `https://www.google.com/`. |
| `DATABASE_URL` | Yes | SQLAlchemy database URL. |
| `KEITARO_TIMEOUT_SECONDS` | No | Request timeout; default 30 seconds. |

At backend startup, validate that required settings exist and that IDs are positive integers. Validate the redirect URL’s scheme and syntax. Do not make mutating Keitaro calls during startup.

The API key must come from the environment or a secret manager. It must not appear in source control, this specification, frontend assets, API responses, or logs.

## 6. Functional requirements

### 6.1 Campaign list and entry points

The application provides:

1. A **New campaign** form.
2. A list of locally known campaigns with name, Keitaro ID, dirty state, and last synchronization time.
3. An **Open by Keitaro campaign ID** action for importing an existing campaign.
4. An editor route for each local campaign.

Successfully creating a campaign opens its editor with a clean baseline.

### 6.2 New campaign form

Fields:

| Field | Validation |
|---|---|
| Campaign name | Trimmed, non-empty, maximum 255 characters. |
| Country code | ISO 3166-1 alpha-2; normalize to uppercase, for example `DE`. |
| Keitaro offer ID | Positive integer referencing an existing, non-deleted Keitaro offer. |

Domain, group, traffic source, and Google URL are not form fields; their configured IDs and value are reused for every new campaign.

Before creating anything, the backend must verify that:

1. The configured domain, group, and traffic source exist and are usable.
2. The requested offer exists and is not deleted.
3. All campaign and stream payloads pass local validation.

### 6.3 Created campaign structure

Create an active Keitaro campaign with:

- The supplied name.
- A unique, non-empty, URL-safe alias generated by the backend. The target
  instance rejects empty aliases despite the public API reference describing
  automatic generation.
- `domain_id`, `group_id`, and `traffic_source_id` from application settings.
- Flow rotation type `weight`, enabling stream-weight split testing.
- Exactly two active streams with initial shares of 50% each.

Create streams in this order:

1. **Google — `{GEO}`**
   - Type `regular`, position 0, weight 50, state `active`.
   - Redirect schema and the Keitaro redirect action supported by the target instance.
   - Redirect target is `GOOGLE_REDIRECT_URL`.
   - Has a country filter in `accept` mode with payload `[GEO]`.
   - Has no offers.

2. **Offer — `{OFFER_ID}`**
   - Type `regular`, position 1, weight 50, state `active`.
   - Offer-capable schema (`landings` in the public API), with no landing-page requirement added by this application.
   - Contains the selected offer with association state `active` and offer share 100.
   - Has a country filter in `reject` mode with payload `[GEO]`.

The complementary filters are mandatory. They ensure that traffic from the selected country can match only the Google stream and all other traffic can match only the offer stream. The 50/50 weights must not be interpreted as splitting traffic within either geographic segment.

The Keitaro adapter must verify the exact action field names against the target instance’s Admin API rather than assuming that every Keitaro version uses identical redirect payload fields.

### 6.4 Creation failure and retry behavior

Keitaro campaign creation is not atomic:

1. Create the campaign.
2. Create the Google stream.
3. Create the offer stream.
4. Fetch the resulting campaign and streams and store them as the baseline and draft.

If any step fails, stop immediately. For a newly created campaign that has not yet received a complete stream setup, attempt to archive it as compensating cleanup. Report whether cleanup succeeded and include the Keitaro campaign ID if it exists.

A create request must accept an idempotency key. Repeating the same request with the same key must return the original result instead of creating a duplicate campaign. A different payload with the same key returns HTTP 409.

### 6.5 Fetching an existing campaign

Fetching must retrieve the campaign and all its non-deleted streams, including disabled streams, with their offer associations and remote timestamps.

On an operator-initiated fetch:

1. Replace the baseline with the fetched remote state.
2. Replace the draft with the same state.
3. Clear all pins.
4. Normalize disabled stream shares to zero.
5. If active shares do not total exactly 100%, redistribute them equally and mark the draft dirty with an explanatory notice.
6. Normalize active offer shares within each offer-based stream in the same way.
7. Store the remote fingerprint and increment the local draft revision.

A normal fetch is allowed only when the draft is clean. If it is dirty, return HTTP 409 and require the operator to choose **Discard changes and reload**. That action requires confirmation and then replaces the draft. A fetch in `syncing` is also rejected without confirmation; a confirmed hard reset may recover a stuck `syncing` state after acquiring the per-campaign operation lock, ensuring it cannot overlap a live upload.

Remote-deleted streams are not restored into the editor. If a previously known stream is absent because it was deleted remotely, the fetch result must note that fact for diagnostics.

### 6.6 Streams table

Display one row per fetched stream, including disabled rows.

Columns:

- **Name**: remote stream name plus a visible `Disabled` badge when applicable.
- **Offers**: active offer count and offer editing for offer-capable streams.
- **Actions**: `Delete` for an active stream or `Restore` for a disabled stream.

Additional behavior:

- Sort by remote position, then Keitaro stream ID.
- Disabled rows are visually distinct.
- Disabled streams have share 0.
- Deleting a stream changes local state to `disabled`; it never removes the local row.
- Restoring changes local state to `active`.
- Stream names are read-only.
- The editor does not create new streams.
- Unsupported remote stream fields must be preserved by sending partial updates only for managed fields.

Offer lists are expanded by default and can be collapsed per stream. Offer-based streams support adding, removing, and pinning offers. Pure redirect/action streams, including the Google stream, do not expose offer editing.

### 6.7 Stream share calculation

All application-facing stream shares are integers from 0 through 100. The backend, not React, is authoritative for recalculation.

Definitions:

- `A`: active streams, sorted by position and then Keitaro stream ID.

Rules:

1. Disabled streams always have share 0 and are excluded from `A`.
2. Divide 100 equally over `A`:
   - `base = floor(100 / len(A))`.
   - `remainder = 100 mod len(A)`.
   - Assign `base + 1` to the first `remainder` streams in deterministic order and `base` to the rest.
3. Active shares must total exactly 100 after every mutation.
4. A zero share is valid; an active stream with zero share receives no weighted rotation traffic.

Recalculate after deleting or restoring a stream. Stream shares are always redistributed equally; pinning exists only at the offer level (section 6.8).

Examples:

- Three active streams: `34, 33, 33`.
- Deleting one of three streams leaves `50, 50`.

### 6.8 Offer editing

For each offer-based stream:

1. Show active associated offers with their integer shares, a visible `Pinned` badge where applicable, and a Pin/Unpin control.
2. Provide **Add offer**, which opens a searchable selector of existing non-deleted Keitaro offers.
3. Exclude offers already actively associated with that stream.
4. On add, create or reactivate one local association and redistribute the active offer shares in that stream.
5. On remove, mark only the local association disabled and remove it from the active list. On upload, delete that association from the Keitaro stream while retaining the disabled local row. Do not delete or modify the global Keitaro offer.
6. Re-adding a previously removed offer reactivates its local row. If the remote association was deleted by a prior upload, create a fresh remote association instead of reusing its old Keitaro association ID.
7. An active offer-based stream must contain at least one active offer before upload. The UI may allow the last offer to be removed while the stream is disabled, but restoring that stream requires adding an offer first.

Changing offers within one stream does not change stream-level shares.

#### Offer share calculation and pinning

Definitions (per offer-based stream):

- `A`: active associations.
- `P`: sum of shares of active pinned associations.
- `U`: active unpinned associations, sorted by Keitaro association ID and then offer ID (locally added associations, which have no Keitaro ID yet, sort first).
- `R = 100 - P`: share available to `U`.

Rules:

1. Removed (disabled) associations always have share 0 and no pin, and are excluded from `A`, `P`, and `U`.
2. `P` must be between 0 and 100 inclusive.
3. If `U` is non-empty, divide `R` equally with the same deterministic integer rounding as stream shares, in `U` order.
4. Active pinned shares remain unchanged.
5. If `A` is non-empty and `U` is empty, the state is valid only when `P = 100`.
6. Active shares must total exactly 100 after every mutation.
7. A zero share is valid but must be visually indicated because that active offer receives no rotation traffic.

Recalculate after adding, removing, pinning, or unpinning an offer. Reject any operation that would make rule 5 impossible; for example, removing the last unpinned active offer while pinned shares total less than 100.

Pinning records the offer's current computed share and redistributes the remainder. Unpinning makes that offer eligible for equal redistribution. Removing an offer clears its pin; a re-added offer starts unpinned. Pin metadata alone is not sent to Keitaro. If toggling a pin does not change any remote-managed value, it does not by itself make the draft dirty.

Examples:

- Three unpinned offers: `34, 33, 33`.
- Shares `40, 30, 30`, with the first pinned: `40, 30, 30`.
- Pinning a second offer at 30 leaves the final unpinned offer at 30.
- If two pinned offers total 70 and two unpinned offers remain, the unpinned offers receive 15 each.

### 6.9 Local draft persistence and dirty state

Every successful local mutation is persisted to the relational database immediately. The UI may update optimistically, but it must roll back and show an error if persistence fails.

Dirty state is derived by comparing canonical Keitaro-managed draft data with the baseline. It includes:

- Stream state and weight.
- Offer association state and share.
- Any other field explicitly made editable in a future version.

It excludes pins, UI expansion state, and other local-only metadata.

Each campaign has a monotonically increasing local draft revision. Draft update requests must include the revision they read. A stale local revision returns HTTP 409 so two tabs cannot silently overwrite each other’s drafts.

### 6.10 Synchronization button and editor states

The primary button above the streams table behaves as follows:

- Clean draft: **Get streams from Keitaro**.
- Dirty draft: **Upload streams to Keitaro**.
- Operation in progress: disabled, with a loading indicator.

Other states and actions:

- **Discard changes and reload** is available for a dirty draft and requires confirmation.
- **Hard reset from Keitaro** is available for clean drafts and blocked synchronization states, including `syncing` and `needs_attention`. After confirmation, it discards all local changes, fetches the current Keitaro streams, and replaces the local baseline and draft.
- Pin changes that do not affect remote-managed data leave the button in its clean state.
- Validation errors appear next to the affected stream or offer and disable upload.
- A remote conflict displays a clear message and a reload action; it never silently overwrites either version.
- After a successful upload, fetch the remote state, replace the baseline and draft, retain compatible pin metadata, and show the clean state. If normalization is still necessary, show the resulting dirty state rather than claiming success with different values.

## 7. Synchronization and conflict handling

### 7.1 Remote fingerprint

After every fetch, calculate and store a SHA-256 fingerprint of a canonical representation containing:

- Campaign ID and remote campaign update timestamp.
- Every managed stream’s ID, remote update timestamp, state, position, weight, schema, type, filters, and action data.
- Every offer association’s ID, offer ID, state, and share.

Canonicalization must sort object keys and collections by stable remote IDs and must not depend on JSON formatting. Fields with no bearing on campaign configuration may be excluded only after verifying that changes to them also update one of the included timestamps.

### 7.2 Upload sequence

Before any remote write:

1. Lock the campaign’s local synchronization operation so only one upload can run at a time.
2. Re-read and validate the persisted draft.
3. Fetch the current remote campaign and streams.
4. Calculate the current remote fingerprint and compare it with the stored baseline fingerprint.
5. If different, return HTTP 409, perform no remote writes, and preserve the local draft.
6. If equal, update only streams and associations whose managed fields differ from the baseline.

Use Keitaro partial `PUT` updates. Do not replace the entire campaign or resend unrelated configuration. Map local stream state to Keitaro `active`/`disabled`, stream percentage to `weight`, and offer percentage to association `share`. Whenever offer associations change, send the stream's complete desired active-offer collection. Removed associations are omitted so Keitaro deletes them from the stream; they remain disabled only in the local draft. Compensation restores the baseline offer collection, recreating removed associations and deleting newly added ones by omission.

Keitaro does not document a transactional compare-and-set operation. The fingerprint preflight is therefore optimistic conflict detection, not a guarantee against a write occurring between the preflight and update calls. The UI and documentation must not claim fully atomic synchronization.

### 7.3 Partial upload failure

If an upload fails after one or more remote updates:

1. Stop making further updates.
2. Attempt to restore already changed remote streams to their baseline values.
3. If rollback succeeds, keep the draft dirty and report that no intended draft remains partially applied.
4. If rollback fails or its result cannot be confirmed, mark the campaign `needs_attention`, block further uploads, and require a fresh fetch before any new draft work.
5. Show which operation failed without exposing credentials or raw secret-bearing headers.

## 8. Backend application API

The exact URL prefix may follow project conventions; `/api/v1` is recommended.

| Method and path | Purpose | Important responses |
|---|---|---|
| `GET /campaigns` | List locally known campaigns and derived sync state. | 200 |
| `POST /campaigns` | Validate and create a campaign remotely and locally. Requires `Idempotency-Key`. | 201, 400, 409, 502, 504 |
| `GET /campaigns/{id}` | Return baseline metadata, draft, pins, revision, and dirty state. | 200, 404 |
| `POST /campaigns/import` | Import a clean campaign by `keitaro_campaign_id`. | 201, 400, 404, 409, 502, 504 |
| `POST /campaigns/{id}/fetch` | Fetch a clean draft; optionally discard a dirty draft only with explicit confirmation flag. | 200, 409, 502, 504 |
| `PUT /campaigns/{id}/draft` | Atomically replace the editable draft after validating `expected_revision`; recalculate shares server-side. | 200, 400, 409, 422 |
| `POST /campaigns/{id}/upload` | Run conflict preflight and push the persisted dirty draft. | 200, 400, 409, 502, 504 |
| `GET /offers?search=` | Return existing selectable Keitaro offers. | 200, 502, 504 |
| `GET /health` | Report application health without exposing configuration secrets. | 200, 503 |

Campaign create request:

```json
{
  "name": "Example DE",
  "country_code": "DE",
  "offer_id": 123
}
```

Draft responses must include both the editable stream/offer data and the current local revision. Upload operates on the persisted draft rather than trusting a complete campaign payload supplied at upload time.

Error response shape:

```json
{
  "error": {
    "code": "remote_conflict",
    "message": "The campaign changed in Keitaro. Reload it before uploading again.",
    "details": {}
  }
}
```

Use HTTP 400 for malformed requests, 404 for missing local or remote resources, 409 for stale revisions/remote conflicts/dirty fetch attempts, 422 for valid JSON that violates campaign rules, 502 for invalid or unsuccessful Keitaro responses, and 504 for Keitaro timeouts.

## 9. Keitaro API integration

Use the Admin API with the `Api-Key` header, sent only by the backend.

Primary external endpoints:

- `GET/POST /campaigns`; `GET/PUT /campaigns/{id}`.
- `POST /streams`; `GET/PUT /streams/{id}`.
- `POST /streams/{id}/disable` and the corresponding enable operation when available; otherwise partial state updates.
- `GET /offers` and `GET /offers/{id}`.
- `GET /domains`, `GET /groups`, and `GET /traffic_sources` for configuration validation.

Relevant public schema mappings:

- Campaign creation accepts `name`, `alias`, `type`, `group_id`, `traffic_source_id`, and `domain_id`.
- Campaign type `weight` enables flow split testing.
- Stream creation accepts `campaign_id`, `schema`, `type`, `name`, `position`, `weight`, `state`, filters, and nested offers.
- Nested offer associations use `offer_id`, integer `share`, and state `active` or `disabled`.

Integration rules:

- Apply connection/read timeouts to every call.
- Retry safe reads only, with bounded exponential backoff for timeouts, HTTP 429, and transient 5xx responses.
- Do not blindly retry campaign creation. Use the application idempotency record and inspect the prior result first.
- Validate response types and required IDs before persisting anything.
- Treat unexpected successful responses with missing required data as integration failures.
- Log operation name, campaign/stream IDs, duration, and status category, but never the API key or request authorization headers.

References:

- [Keitaro Admin API guide](https://docs.keitaro.io/en/development/admin-api.html)
- [Keitaro Admin API reference](https://admin-api.docs.keitaro.io/)

## 10. Data model

The implementation may adjust physical names, but it must preserve these relationships and semantics.

### `campaigns`

- Local UUID primary key.
- Unique Keitaro campaign ID.
- Name and created campaign country code where available.
- Baseline canonical snapshot and baseline fingerprint.
- Draft revision and synchronization state (`clean`, `dirty`, `syncing`, `needs_attention`; clean/dirty may instead be derived).
- Last successful fetch and upload timestamps.
- Creation idempotency key and request hash, unique when present.
- Created and updated timestamps.

### `streams`

- Local UUID primary key and campaign foreign key.
- Keitaro stream ID, unique per campaign.
- Name, type, schema, position, state, and integer draft share.
- Baseline raw managed payload or sufficient data to construct partial updates and fingerprints.
- Created and updated timestamps.

### `stream_offers`

- Local UUID primary key and stream foreign key.
- Nullable Keitaro association ID for a newly added association.
- Keitaro offer ID and display-name snapshot.
- Association state and integer draft share.
- Local `pinned` boolean.
- Baseline association data where present.
- Unique active association constraint semantics for `(stream_id, offer_id)`.
- Created and updated timestamps.

Store the baseline separately from mutable draft rows so reset, dirty comparison, conflict detection, and rollback remain possible. Database transactions must make each local draft replacement atomic.

## 11. Security and operational requirements

- Never expose the Keitaro credential to React or include it in URLs.
- Restrict CORS to known frontend origins.
- Validate all IDs and percentages server-side even when the UI already validates them.
- Use parameterized SQLAlchemy operations and database foreign keys.
- Treat Keitaro responses as untrusted external data; escape names and URLs in React.
- Do not log request headers, credentials, or full external response bodies by default.
- If the application is exposed to an untrusted network, add authentication and HTTPS before deployment. The single-user assumption is not a substitute for network security.
- Use structured logs and distinguish validation, conflict, timeout, remote rejection, and partial-sync failures.

## 12. Acceptance criteria

### Campaign creation

1. A valid form creates one campaign with the configured domain, group, and traffic source, weighted rotation, and exactly two active streams.
2. The first stream accepts only the selected country and redirects to the configured Google URL.
3. The second rejects that country and sends all remaining matching traffic to the selected offer at 100% offer share.
4. Initial stream shares total 100%.
5. Invalid country codes, missing settings, and unavailable offers create no campaign.
6. Repeating a create request with the same idempotency key does not create a duplicate.

### Fetching and persistence

7. Importing an existing campaign displays active and disabled streams and their offers.
8. Draft edits survive a browser reload and backend restart.
9. Fetching a dirty draft without confirmation returns 409 and changes neither baseline nor draft.
10. Discarding changes replaces the draft with the fetched remote state.

### Shares and offers

11. Active stream shares total exactly 100 after every delete and restore operation; active offer shares total exactly 100 after every add, remove, pin, and unpin operation.
12. Disabled streams remain visible, have zero share, and can be restored.
13. Pinned offer shares remain fixed while unpinned active offers divide the remainder using deterministic rounding.
14. Invalid operations that leave no unpinned active offer and pinned shares below 100 are rejected without changing the draft.
15. Adding, removing, and re-adding offers maintains equal active offer shares within the selected stream and never modifies the global offer.
16. An active offer-based stream without an active offer cannot be uploaded.

### Synchronization

17. A local Keitaro-managed edit changes the primary button from **Get streams from Keitaro** to **Upload streams to Keitaro**.
18. A clean upload is not required; a dirty upload updates only changed remote-managed fields.
19. If the remote fingerprint differs, upload returns 409 and performs no Keitaro writes.
20. After a confirmed successful upload, the fetched baseline matches the uploaded values and the editor is clean.
21. A partial failure either rolls back successfully or blocks further uploads with `needs_attention`.
22. No API response, frontend asset, or application log contains the Keitaro API key.

## 13. Verification strategy

- Unit-test percentage distribution, rounding order, pin edge cases, dirty comparison, and fingerprint canonicalization.
- Test backend draft persistence and revision conflicts using the project’s selected database.
- Use a mocked Keitaro HTTP API for creation, fetch, upload, remote conflict, timeout, malformed response, and partial-failure scenarios.
- Assert that no remote write occurs when validation or conflict preflight fails.
- Run a manual end-to-end check against a dedicated disposable Keitaro test campaign only with explicit operator approval. Do not use production campaigns for verification.
- Verify both geographic routing paths and weighted offer distribution in Keitaro after creation, not only the API response bodies.

## 14. Resolved decisions and assumptions

- Selected-country traffic goes to Google; all remaining traffic goes to the selected offer.
- Domain, group, traffic source, and Google URL are application-wide backend settings.
- Offers are managed per stream with equal active offer shares.
- Remote conflicts reject the upload and require a refresh; no automatic merge or overwrite occurs.
- Percentages are integer values with deterministic rounding.
- Pinning is local-only offer metadata; removed offers lose their pins and re-add as unpinned. Stream shares always redistribute equally.
- The initial application is single-operator and has no background synchronization.
