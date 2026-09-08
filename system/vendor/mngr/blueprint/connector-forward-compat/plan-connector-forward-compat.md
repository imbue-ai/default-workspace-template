# Connector client forward compatibility

## Overview

- Every Python model that parses a `remote_service_connector` response inherits `FrozenModel` (`extra="forbid"`), so any additive server change breaks the entire shipped desktop fleet -- hard failures on single-object endpoints, and silently empty listings on per-entry-validated list endpoints. The TS clients (web chrome, accounts pages) are already tolerant.
- Adopt AWS-style, effectively indefinite backward compatibility for the imbue cloud surface: clients tolerate unknown response fields and unknown enum values by construction, and the connector's own CI proves every response still parses for every shipped client version inside the support window (~1 month now, extending to 3-6 months later).
- Core mechanisms: tolerant `WireModel` / `WireEnum` bases in `mngr_imbue_cloud` enforced by ratchets; automatic `UNKNOWN` enum coercion at the parse boundary; strict list-failure semantics (skipping must never fabricate an empty fleet); a canonical `X-Imbue-Client` header from every client; a golden compat test in the connector validating live responses against vendored, dated old-client model snapshots; and preserve-unmentioned-columns sync merge with two-level format write-locks (`record_format` plaintext column, `payload_format` inside the encrypted blob).
- Explicitly rejected: server-side version-gated response shaping (the existing tunnel-era shim stays `CLEANUP`-tagged and is removed after the forced update). Future forced deprecations use a reserved structured HTTP 426 refusal plus a later version/update route; this work only defines the shape and teaches clients to render it.
- Scope note: this contract covers only the imbue cloud provider and its associated workspaces/resources. minds is open source and does not strictly depend on imbue cloud (login plus extras like backups, cloud workspaces, sharing).

## Expected behavior

Old client, new server (the forward-compat contract):

- A new field on any connector response is ignored by every client; the plugin debug-logs the unknown keys once per (model, key-set) per process. Removing or renaming a required response field still fails loudly (required fields stay strict) -- additions are tolerated, removals are a breaking change caught by the compat test before deploy.
- An unknown enum value coerces to that enum's `UNKNOWN` member at the parse boundary. A workspace with `WorkspaceStatus.UNKNOWN` maps to mngr's existing `HostState.UNKNOWN` and the minds UI shows it with state-changing actions disabled and an "update the app to manage this machine" hint. An unknown `R2BucketAccess` behaves as `read`.
- List endpoints: a single unparseable entry is skipped with a warning log; a non-empty response where every entry fails raises (the provider surfaces an error instead of reporting a silently empty fleet, preserving mngr's mark-UNKNOWN-on-provider-failure safeguard); a non-list body raises instead of degrading to `[]`.

Client identification:

- Every connector call carries `X-Imbue-Client` as the canonical client identifier: `minds/<release> imbue-cloud-plugin/<package-version>` from the desktop stack (product half omitted for standalone `mngr imbue_cloud` CLI use), `web/<deploy-id>` from both connector-served bundles (accounts pages and web chrome). The Python client mirrors the same string into `User-Agent`; the connector's access log records both.
- The reserved "client too old" refusal (HTTP 426, `{code: "client_too_old", min_version, sunset_date, message}`) is rendered meaningfully by current clients (desktop UI banner, CLI message with upgrade hint). Enforcement machinery ships later with the version/update route.

Workspace-record sync:

- The server merges record PUTs preserve-on-absent: a field absent from the push keeps its stored value; an explicit `null` clears it. Old clients send every field they know, so their observed behavior is unchanged -- but a field they do not know about survives their pushes instead of being reset.
- Records carry a plaintext `record_format` (absent/missing = 1; bumped only for semantically breaking changes -- purely additive display fields ride preserve-on-absent without a bump). A client seeing a record whose format exceeds its support treats it as read-only: no record pushes, no destroy/tombstone, no release, no record deletion (disassociation) -- all surfaced as "update the app to manage this machine". Connecting to the workspace and stop/start remain allowed (state *changes to the record's meaning* are what is locked; account-wide scrub-secrets also remains allowed as a deliberate, format-independent destructive action).
- The server enforces the lock too: a PUT whose `record_format` is below the stored row's is rejected with a structured 409 (`record_format_too_new`); the client-side check is friendly UX in front of that guard.
- The encrypted secrets blob carries its own `payload_format` inside the ciphertext. A client never rewrites a blob whose `payload_format` exceeds its support, and when it does rewrite, unknown keys round-trip verbatim (the raw dict is the source of truth; the typed payload model is a view). This closes the existing hazard where a re-encrypt through the tolerant payload model would silently drop newer fields.
- The web chrome applies the same write-locks and additionally nudges "a new version is available -- reload" when its baked bundle stamp no longer matches the live `/version` deploy id (the stale-open-tab case).

Compat proof and lifecycle:

- The connector's CI runs a golden compat test: the app runs in-process against fake stores, every client-consumed route is exercised, and each response is validated against every vendored old-client model snapshot in the corpus. Route enumeration is automatic -- a new client-consumed route with no compat fixture fails the test (routes can be explicitly marked client-unconsumed).
- Snapshots are self-contained files (their strict base config inlined, no imbue_common import) stamped with their release date; a snapshot older than the support window fails the test with a "prune or extend" message, so un-freezing is always a deliberate decision. The corpus is seeded with the current shipped strict release now (freezing response shapes in CI until the forced update) and gains the first tolerant release when cut; the release process appends a snapshot per minds release.
- Until the forced update completes: no field additions or removals on existing response shapes; new data ships as new endpoints (old clients never call them). This escape hatch remains documented for future emergencies. After the forced update: the tunnel-era serve-zeros shim and the client-side tunnel compat fields are removed, and the strict-release snapshot is pruned.

## Changes

`libs/mngr_imbue_cloud` (the connector client and its models):

- Add `WireModel` (frozen, `extra="ignore"`) and `WireEnum` (`_missing_` -> `UNKNOWN`) bases.
- Split the connector-response models out of `data_types.py` (currently past the 500-line threshold anyway) into a dedicated `wire_types.py` whose single rule is "everything here is a WireModel"; internal models (CLI reports, DB mirrors, request objects) stay strict.
- Re-base all response models (`AuthRawResponse`, `LeaseResult`, `WorkspaceInfo`, `LeasedHostInfo`, `AccountInfo` and nested, `LiteLLMKey*`, `R2Bucket*`/`R2Key*`, `StorageCleanupGrant`, `StorageRecheckResult`, `SyncWorkspaceRecord`, `SyncKeyBundle`, `RelayAdminInfo`, ...) onto `WireModel`; convert `WorkspaceStatus` and `R2BucketAccess` (and record-state interpretation) to `WireEnum` with `UNKNOWN` members; validated strings remain for client->server request values only.
- Add the `validate_wire` helper (validation + once-per-process unknown-key debug logging) and route all `model_validate` calls of HTTP bodies through it.
- Fix list semantics in `connector/client.py` (skip-with-warning per entry, raise on all-failed or non-list bodies) and remove the silent `return []` fallbacks.
- Send `X-Imbue-Client` (and the mirrored `User-Agent`) on every connector call; render the reserved 426 refusal as a typed error with the upgrade message.
- Implement the client half of `record_format` / `payload_format`: supported-format constants, read-only treatment of too-new records, refuse-to-rewrite of too-new blobs, raw-dict round-trip for blob edits.

`apps/minds` (desktop client):

- Re-base the second-layer CLI-parse models in `imbue_cloud_cli.py` onto the same `WireModel`/`WireEnum` bases.
- Surface `HostState.UNKNOWN` workspaces with actions disabled plus the "update the app to manage this machine" hint; the same hint for write-locked (too-new `record_format`) records, including blocked destroy/release/disassociate flows.
- Render the 426 refusal in the UI; keep Sentry release tagging as the version source for the header (already present via `MINDS_RELEASE_ID`).

`apps/remote_service_connector` (server):

- Sync merge: preserve-on-absent semantics via sent-field detection, explicit-null clears, dynamic update column list; unchanged CAS/409 shapes. New `record_format` column (missing = 1) with the `record_format_too_new` 409 guard; migration included.
- Golden compat test: in-process app + fake stores, auto-enumerated client-consumed routes, vendored self-contained dated snapshots, aging enforcement, seeded with the current shipped release.
- Record `X-Imbue-Client` in the access log alongside `User-Agent`.
- `CLEANUP` tags verified on the tunnel-era shim with the new checkable removal condition (no in-window client below the first tolerant release); removal happens post-forced-update.

`frontend_web` + `frontend` (web clients):

- Inject the minds deploy id as the bundle stamp at build time; send `X-Imbue-Client: web/<deploy-id>` from both bundles.
- Web chrome: `record_format`/`payload_format` write-locks, unknown-key-preserving blob rewrites, and the stale-bundle reload nudge against `/version`.
- Pin the existing spread-based record round-trip with a test so a refactor cannot reintroduce field-dropping.

Repo-wide:

- `style_guide.md`: a "wire models" section beside the events schema-evolution section (additive-with-defaults, `extra="ignore"`, `UNKNOWN` members, list semantics, new-endpoint-over-new-field escape hatch).
- `test_meta_ratchets.py`: EventEnvelope-style ratchets banning `extra="forbid"` on `WireModel` subclasses and non-`WireEnum` wire enums; a project ratchet restricting `connector/client.py` `model_validate` calls to `WireModel` subclasses.
- Behaviors: a small set of .feature entries in the relevant corpora for the compat invariants (unknown fields tolerated, all-entries-failed listings raise, write-lock rules).
- Release process (`docs/release.md` / `release-minds` skill): append a compat snapshot per minds release; prune when a release exits the support window.
