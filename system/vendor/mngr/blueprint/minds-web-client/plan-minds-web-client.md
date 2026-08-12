# Minds web client (hosted, browser-only minds)

## Implementation status (in progress)

Landed and unit-tested so far (backend keystone):

- **default-workspace-template** (separate PR): share-gateway made embeddable
  (`SameSite=None; Secure; Partitioned` session cookie, `frame-ancestors` +
  site-wide `/_health` with CORS, owner claim carried from the broker handoff
  into the session and honored on every request); new `owner-exec` service
  (Ed25519-signed run/read-file/write-file/grants against `authorized_keys`);
  `provision_backups.py` for the in-workspace restic init.
- **connector**: broker owner fast path (silent handoff + `owner:true`, no
  verified-email gate for owners); `POST /hosts/{id}/enable-sharing` (pool-key
  server-side share bring-up with `SHARE_CHROME_ORIGIN` + owner grants).

Not yet implemented (the larger front-end / integration pieces, deferred as
they need a browser and real pool/Modal infra to verify): `POST /hosts/claim`
(lease + adopt-over-SSH + compose enable-sharing) and its pinned-template
config; the workspace LiteLLM mint endpoint + chrome bundle serving; the entire
`frontend_web` chrome SPA (crypto, records/CAS, exec client, views); the
desktop auto-share toggle + grants single-writer migration + tombstone-safety
test. These remain as written in the sections below.

## Overview

- Ship a minimal hosted web client for minds at `minds.imbue.com`: sign in, see your workspaces, create new ones, open them in an embedded iframe, and destroy them -- no desktop app required.
- The browser tab is the orchestrator. There is no hosted per-user backend and no hosted minds API: the chrome SPA talks directly to the remote service connector (same-origin) and to workspaces (over the existing share stack).
- Workspace access rides the self-hosted share stack unchanged in shape: every web-reachable workspace is one that has been shared, with the owner auto-granted. "Shared with yourself" is the web access model.
- Server-side workspace touch is confined to the connector using the tier pool management key, via two synchronous primitives: `POST /hosts/claim` (lease + adopt + share bring-up, for create) and enable-sharing (share bring-up alone, for workspaces created elsewhere). Everything after claim is browser-driven.
- The browser gets SSH-equivalent authority through a new in-workspace `owner-exec` service: run-command / read-file / write-file behind the share gateway, with every request Ed25519-signed and verified against the workspace's `authorized_keys` -- the same key set that governs SSH.
- The E2E secrets model is preserved: the browser generates the workspace SSH keypair, holds the account DEK (master password required, unlocked at sign-in), encrypts secrets client-side, and writes sync records itself. The server never holds a workspace private key or the DEK.
- Backup provisioning moves in-workspace (an init script driven over exec); the desktop's client-side restic orchestration is not needed for web-created workspaces. Verification/trim/restore/export are deferred.
- Fast-path leases only, one blessed compute shape per tier, template repo/tag pinned server-side to the current release. Workspaces are assumed always-running in v1.
- No latchkey work: env vars are wired at claim, the VPS gateway is provisioned later by desktop discovery, and web workspaces get only desktop-synced third-party keys.
- v1 audience is internal dogfood: staging + dev tiers first, production only after iteration.
- Cross-repo: connector/chrome/desktop changes land in this monorepo; the `owner-exec` service, share-gateway changes, and backup init script land in default-workspace-template (dwt) and reach users via a pool re-bake. No migration for existing workspaces -- they get recreated.

## Expected behavior

### Signing in and unlocking

- Visiting `minds.imbue.com` with no session lands on the existing hosted accounts sign-in (same `Domain=imbue.com` SuperTokens browser session; single account per browser by construction).
- Immediately after sign-in the chrome prompts for the master password (unlock-at-sign-in): a returning account unwraps its key bundle; an account with no bundle is walked through setting a master password (the web can mint the account's first DEK + bundle).
- A "remember password" checkbox (unchecked by default) persists the unwrapped DEK in IndexedDB (never the password); unchecked, the DEK lives in sessionStorage and the prompt returns in a new tab/session.
- Full master-password parity in v1: unlock, set, change (rewrap DEK + re-push bundle), clear (delete bundle locally and server-side + scrub synced secrets).

### Overview page

- Shows one tile per synced workspace record (plaintext metadata: name, color, provider, state) -- cloud and local rows alike.
- Each tile shows health: HEALTHY / DEGRADED / UNREACHABLE from the share-gateway `/_health` probe, falling back to connector share status + lease state to disambiguate (e.g. "not shared", "destroyed").
- Shared local (docker/lima) workspaces open exactly like cloud ones; lease-only actions (destroy, enable sharing) are hidden for them.
- An owned-but-unshared imbue_cloud workspace shows an "Enable web access" action that calls the connector's enable-sharing endpoint; afterwards it opens normally. Unshared local workspaces show as "desktop-only".
- Destroyed records render as such (no reachability probing).

### Opening a workspace

- Clicking a tile navigates the chrome shell to that workspace: the sandboxed cross-origin iframe loads the workspace's share URL.
- Owner entry is silent: the gateway 302s to the accounts broker, which recognizes the session's user_id as the share owner and redirects straight back with an `owner: true` handoff -- no interstitial, no email-verification requirement. Visitors (non-owners) keep the interstitial and the verified-email gate.
- The workspace session cookie is `SameSite=None; Secure; Partitioned` so it works inside the cross-site iframe; WebSockets (terminal, chat) work through the relay unchanged.
- On browsers where partitioned iframe cookies fail (older Safari), the chrome falls back to opening the workspace in a new tab (top-level = first-party; works everywhere). The chrome is responsive; mobile browsers get the same fallback behavior.
- The chrome handles the embed contract's `minds:open-ai-keys-page` (ack + hosted mint modal); other inbound message types are console.log'd and tracked as future tasks.

### Creating a workspace

- The create form asks for: workspace name, region (geo-defaulted), and nothing else. Template repo/tag and compute shape are pinned server-side.
- Submit runs the browser-orchestrated sequence with visible progress: generate keypair -> persist pending-create (key encrypted under DEK, in IndexedDB) -> `POST /hosts/claim` -> push sync record (revision 1, encrypted secrets) -> poll share `/_health` until up -> provision backups over exec -> land in the workspace.
- Web creates always bring sharing up inside claim (the exec channel is required to finish setup). The desktop's create form gains an "enable web access" toggle for the same behavior, unchecked by default there.
- The new workspace boots unauthenticated, exactly like desktop creates: the in-workspace sign-in modal offers Claude OAuth, and "Sign in with Imbue" opens the chrome's hosted mint modal (mints a LiteLLM key against the owning account, renders the paste-ready env blob).
- A tab closed mid-create is recovered on the next visit: the pending-create record + `GET /hosts` reconcile lets the user resume (re-push record, re-run setup) or discard (release the lease). Browser-only cleanup in v1.
- Quota errors (`max_remote_workspaces`, share cap) surface as actionable messages.

### Destroying a workspace

- Destroy (imbue_cloud rows only) asks for confirmation (plain confirm dialog, matching the desktop), then: connector lease release (server-side slice VM teardown, existing endpoint) + tombstone the sync record (state=destroyed). Backup buckets follow the existing 30-day retention reaper.

### Interactions with the desktop app

- A desktop signed into the same account sees web-created workspaces via the normal record sync: it materializes the synced SSH key, discovers the host, enriches the record, and provisions the VPS latchkey gateway on first discovery (web workspaces gain desktop-synced third-party keys only then).
- Grants become workspace-owned: both desktop and web edit sharing grants through a workspace endpoint instead of desktop-side `mngr exec` rewrites; the workspace is the single writer of `share_grants.toml`.
- Record writes from two live clients are safe: CAS + field-ownership merge rules (user-intent fields win, discovery-derived fields enrich-only, secrets gated on plaintext content hash), read-modify-write per action.
- A desktop must never tombstone a web-created workspace it has not yet materialized keys for: cloud-row "definitively absent" requires the lease to be gone, verified by a dedicated test.

### Security posture (documented in this repo as part of the work)

- The exec channel requires possession of the workspace private key (request signatures verified against `authorized_keys`), not merely the workspace session cookie -- authorization is equivalent to SSH.
- The chrome ships a strict CSP (no third-party JS) and documents the DEK-in-tab threat model.
- The connector's pool-key access to slices (already used for lease key injection and teardown) now also covers adopt + share-materials injection; stated explicitly in the security docs.

## Implementation plan

### default-workspace-template (separate repo, separate PRs)

- `system/services/owner_exec/` (new service, per-service origin `owner-exec.<domain>`, supervisord `[program:owner-exec]`):
  - Small HTTP/WebSocket server on loopback, registered via `forward_port.py` into `apps.toml` like other services.
  - Endpoints: `run` (streamed stdout/stderr + exit code over WS), `read-file`, `write-file` (atomic, mode-preserving).
  - Auth: verifies an Ed25519 signature envelope (method, path, body hash, workspace domain, timestamp, nonce) against the container's `~/.ssh/authorized_keys`. The domain binding makes a captured envelope useless against any other workspace. Timestamps outside a +/-60s window are rejected; nonces are tracked in an in-memory cache scoped to that window. Sits behind the share gateway's forward_auth (owner session) as defense in depth.
- `system/services/share_gateway/`:
  - `server.py`: accept the broker handoff's `owner` claim and carry it into the session cookie; add `/_health` (unauthenticated bare liveness 204 + session-authed detail JSON reporting backend health); emit CORS headers for the configured chrome origin on `/_health`; set the session cookie `SameSite=None; Secure; Partitioned` (was `Lax`).
  - `caddyfile.py`: append `Content-Security-Policy: frame-ancestors 'self' <origin-family> <chrome-origin>` to proxied responses.
  - `materials.py`: parse the new `SHARE_CHROME_ORIGIN` value from `share.env` (one value driving frame-ancestors + CORS).
  - New grants-edit endpoint (owner-session-only, bare origin): read/replace `share_grants.toml` -- the single-writer surface both web and desktop use.
- `system/scripts/` (or `system/libs/`): `provision_backups.py` -- in-workspace port of the desktop's `backup_provisioning` core (write canonical restic env under `data/.secrets/`, `restic init`, idempotent re-runs); invoked over exec with connector-minted bucket credentials passed in.
- Bake: new template release tag; pool re-bake so fast-path leases carry the exec service and gateway changes.

### Connector (`apps/remote_service_connector`)

- `hosts.py`:
  - `POST /hosts/claim` (new, synchronous, idempotent): lease (existing logic, caller pubkey injection) + adopt (rewrite `data.json` host name, set `workspace_display_name` label, write host env: `MNGR_HOST_DIR`, `LATCHKEY_GATEWAY`, `REMOTE_SERVICE_CONNECTOR_URL`, etc.) + share bring-up (compose the enable-sharing primitive below). Self-contained port of the plugin's adopt sequence (the connector image cannot import `mngr_imbue_cloud` -- module-isolation ratchet). Returns lease info + workspace domain.
  - `POST /hosts/{host_db_id}/enable-sharing` (new): create/rotate the share record (reuse `shares.py` internals), then SSH the slice with the pool key to write `share.env` (including `SHARE_CHROME_ORIGIN`) + owner-granted `share_grants.toml`. Idempotent; only for the caller's own leased hosts; share quota enforced. No dedicated rate limit in v1 (authenticated, idempotent, own-slices-only).
  - Config: pinned template repo/tag + blessed compute shape per tier as new `deploy.toml` fields, pushed into the connector's Modal secret at `minds env deploy`; advancing the pin is a manual deploy.toml edit folded into the minds release process.
- `share_broker.py`: owner fast path -- when the browser session's user_id matches the share record's owner, skip the interstitial and mint the handoff JWT with `owner: true`; keep the verified-email gate for non-owners only.
- `llm_keys.py` or `accounts.py`: workspace-scoped mint endpoint for the hosted mint page (alias `workspace-<host_id>`, rotate-on-exists, ownership checked against the caller's sync records) -- the connector-side twin of the desktop's `ai_keys.py`.
- `web.py`: serve the chrome bundle at the `minds.imbue.com` custom domain (second Modal custom domain on the same app); route disambiguation between accounts pages and chrome pages.
- `frontend_web/` (new package, sibling of `frontend/`): the chrome SPA.
  - Views: sign-in redirect glue, unlock/set-master-password, overview (tiles + health), create flow (progress + resume/discard), workspace shell (iframe + switcher + new-tab fallback), destroy confirm, mint modal, settings (password change/clear, remember toggle).
  - `crypto/`: argon2id via WASM (hash-wasm), AES-256-GCM via WebCrypto, Ed25519 via WebCrypto with `@noble/ed25519` fallback; wire-compatible with `imbue_common.secret_wrapping` (nonce||ciphertext AEAD blobs, bundle JSON shape).
  - `records/`: connector sync API client with CAS read-modify-write per action + field-ownership merge; pending-create store (IndexedDB); DEK store (IndexedDB/sessionStorage).
  - `exec/`: signed-envelope exec client (WS streaming).
  - `embed/`: reuse `embed_contract.js` (embedder side); handle `open-ai-keys-page`, console.log the rest.
- `minds env deploy`: build `frontend_web/` alongside the accounts bundle; new custom-domain wiring documented in tier setup.

### Minds desktop + plugin (this repo)

- `apps/minds` create form (`ui_api_create.py`, frontend `create.ts`, `agent_creator.py`): "enable web access" toggle (default off); when on, call the connector enable-sharing endpoint post-create (imbue_cloud) or the existing local share flow (docker/lima).
- `apps/minds` `sharing_handler.py` / `share_materials_injection.py`: grants reads/writes move to the workspace's grants endpoint (over the local forward channel); `mngr exec` grants rewrites removed. Share enable/disable for imbue_cloud rows may also delegate to the connector primitive.
- `apps/minds` `workspace_record_store.py`: no algorithm change; add the tombstone-safety test (cloud row with un-materialized SSH key or live lease is never "definitively absent") and any merge-rule tightening the test surfaces.
- `libs/mngr_imbue_cloud`: no required changes for v1 (claim is a connector-side port, not a refactor); optional follow-up to converge the plugin's adopt on shared constants.
- Docs: update `apps/minds/docs/overview.md` / `design.md` sharing sections; new `apps/minds/docs/web-client.md`; security-boundaries notes for the exec channel, pool-key scope, and DEK-in-tab.
- Changelog entries per touched project (`remote_service_connector`, `minds`, dwt's own changelog, `dev` if tooling changes).

## Implementation phases

1. **Spike: embedding compatibility.** Static test page iframing a real shared workspace: partitioned cookie set/send, WebSocket cookie attach, broker leg inside the iframe, per-browser matrix (Chrome/Firefox/Safari/mobile Safari). Output: minimum Safari version + confirmation the new-tab fallback suffices; go/no-go on iframe-first design.
2. **Share path becomes embeddable + owner-aware** (dwt + connector). Gateway: Partitioned cookie, `SHARE_CHROME_ORIGIN` (frame-ancestors + CORS), `/_health`, owner claim in session. Broker: owner fast path. Result: an owner enters their shared workspace silently inside a test-page iframe; health is probeable.
3. **Connector orchestration primitives.** Enable-sharing endpoint, then `POST /hosts/claim`; pinned template/shape config. Result: a scripted client (curl + a keypair) can create a web-reachable workspace end to end without the desktop.
4. **Owner exec + in-workspace backups** (dwt). `owner-exec` service with signed-envelope auth; `provision_backups.py`; grants-edit endpoint; pool re-bake. Result: the scripted client can run commands and configure backups over the share channel.
5. **Chrome SPA v1** (connector `frontend_web/`). Auth/unlock + crypto, overview + health, open/switch with fallback, create flow with resume/discard, destroy, mint modal, settings. Deployed to a dev tier via `minds env deploy`. Result: the full product loop in a browser.
6. **Desktop alignment.** Auto-share toggle, grants single-writer migration, tombstone-safety test, docs. Result: desktop and web coexist safely on one account.
7. **Dogfood hardening.** Deployment tests for the new endpoints + full create path; staging rollout (`minds-staging` chrome domain); polish from internal use. Production deploy deferred until dogfood exit.

## Testing strategy

- **Unit** (per project `_test.py`):
  - Chrome crypto: cross-compatibility vectors against `imbue_common.secret_wrapping` (wrap/unwrap, encrypt/decrypt, bundle JSON) -- fixtures generated by the Python side, asserted in vitest.
  - Exec signature envelope: sign/verify, tamper, stale timestamp, replayed nonce, unknown key (dwt tests).
  - Gateway: owner-claim session handling, `/_health` variants, CORS/frame-ancestors rendering (extends existing `share_gateway` test suites).
  - Broker: owner fast path (owner silent, non-owner interstitial, unverified owner allowed, unverified visitor gated).
  - Claim/enable-sharing: request validation, idempotency, quota, ownership checks (connector test conventions, mock SSH layer).
  - Record merge: field-ownership rules, CAS retry, secrets content-hash gating (TS); tombstone-safety invariant (Python, minds).
- **Integration**: claim -> record push -> reconcile round trip against a mock connector store; pending-create resume/discard state machine in the SPA (vitest with fake connector).
- **E2E (CI, Playwright vs a ci env)**: sign-in, unlock/set password, overview rendering with prebaked record fixtures, mint modal, destroy flow against a fake lease. Full create-through-exec stays out of CI.
- **Deployment tests (operator-run)**: extend `apps/minds/deployment_tests` -- web create end to end (claim, share up, exec, backups init), owner entry, desktop adoption of a web-created workspace, destroy + retention.
- **Manual/tmux + browser verification**: iframe behavior per browser (phase 1 spike doubles as the checklist), mobile Safari fallback, interrupted-create recovery. Not crystallized into pytest (interactive).
- **Edge cases to cover explicitly**: claim retry after partial adopt; tab death at each create step; wrong master password vs corrupt bundle; two tabs unlocking concurrently; desktop and web editing the same record within one CAS window; workspace with share materials but dead tunnel; enable-sharing on an already-shared host (rotate semantics).

## Resolved during refinement

- Exec signature envelope: binds the workspace domain; +/-60s timestamp window; in-memory nonce cache scoped to the window.
- Pinned template tag + blessed shape: per-tier `deploy.toml` fields pushed at `minds env deploy`; manual bump folded into the minds release process.
- Enable-sharing gets no dedicated rate limit in v1 (authenticated, idempotent, own-slices-only; the 50-share cap stands).
- The desktop's imbue_cloud share enable/disable migrates to the connector primitive in phase 6, together with the grants single-writer change.
- Destroy uses a plain confirm dialog, matching the desktop.

## Phase 6 revision (2026-08-10): grants single-writer, reframed

The desktop's grants reads/writes were specced to move from ``mngr exec``
rewrites onto owner-exec's ``GET/PUT /grants``. Investigation reframed this:
the authorization argument is empty (``mngr exec`` is already owner
authority), neither path takes a lock today (both are atomic-rename
last-write-wins, so the real hazard is read-modify-write lost updates, which
the endpoint as specced did not fix either), and mngr's client keypairs were
RSA-4096 while owner-exec verifies only Ed25519 -- so desktop-created
workspaces could not sign exec envelopes at all. Revised plan:

1. **mngr client keygen flipped to Ed25519** (``generate_ssh_keypair``,
   OpenSSH private-key format). Every consumer auto-detects the key type;
   existing RSA keys on disk keep working (generation only happens when no
   pair exists). After the pool re-bake / recreate horizon, the key the
   desktop SSHes with is also a valid owner-exec signing key.
2. **CAS added to owner-exec's grants endpoints** (dwt): ``GET /grants``
   returns a ``revision`` (digest of the file bytes; ``""`` when absent),
   ``PUT /grants`` takes an optional ``base_revision`` and refuses stale
   writes with a 409 carrying the current document. Added now, while the
   endpoint has no live consumers, so the contract never has to change under
   shipped clients. The chrome's ``ExecClient`` threads the revision through.
3. **Connector grants seed is now seed-if-absent**: re-enabling sharing
   rotates ``share.env`` (relay token) but no longer overwrites a grants
   document the workspace already owns.
4. **The desktop's actual switch to the endpoint is deferred** until the exec
   path causes real pain or desktop/web convergence makes it free. The
   ``mngr exec`` grants path remains the desktop mechanism for now; the
   connector's first-enable seed stays a documented out-of-band writer.

## Open questions

- Staging chrome domain naming (`minds.staging-...` vs a staging twin like the accounts domain uses) -- follows the existing accounts-domain convention, confirm at tier setup.
- Minimum Safari version and whether mobile Safari gets iframe or always-fallback -- answered by the phase 1 spike.
- Deferred (tracked, not in v1): connector-side stale-unclaimed-lease sweep; backup verification/trim/restore/export from web; latchkey permission approvals + credential management on web; remaining embed-contract messages (`open-help`, `open-request-modal`, notifications); wake-on-request for stopped workspaces; chrome error reporting (Sentry); multi-account on web.
