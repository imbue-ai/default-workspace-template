# owner-exec: one signed exec channel for containers and VMs

## Overview

- Web-only (browser) minds clients have no SSH, so they cannot configure anything that runs
  *outside* the workspace container on the remote outer host (the imbue_cloud slice VM or a
  VPS): the latchkey gateway, VM-level debugging, key rotation, reboot recovery. Today all of
  that is desktop-SSH-provisioned only.
- Generalize the existing in-container `owner-exec` service into a single small Go daemon,
  developed in a new **public repo `imbue-ai/owner-exec`**, deployed in two roles:
  - **inner**: in the workspace container, replacing dwt's Python `owner_exec` service.
  - **vm**: directly on the remote outer as root, giving the owner VM-level exec authority.
- The auth model is unchanged in spirit: possession of an Ed25519 key present in the *target's
  own* `authorized_keys`. The vm instance verifies against `/root/.ssh/authorized_keys` --
  VM-owned, container-unwritable -- so a compromised container can proxy exec traffic but can
  never forge or execute. (The user's key is already on both endpoints: the connector's
  lease/claim injects it into the VM and the container.)
- The wire format moves from the homegrown v1 envelope to a **strict profile of RFC 9421
  (HTTP Message Signatures) + RFC 9530 (Content-Digest)**, in one shot with no compatibility
  period (nothing outside dev tiers serves web workspaces yet). Existing RFC libraries are
  used where they pass review, versions pinned; self-implement only where one falls short.
- **Responses are always signed** by the endpoint's SSH host key (container host key for
  inner, VM host key for vm), request-bound via `;req` components -- the untrusted container
  path cannot tamper with results either. Clients fail closed on a missing/invalid signature.
- **Audiences become host-id-scoped**: `ct:<host-id>` (inner) and `vm:<host-id>` (vm). This
  kills cross-instance replay (same key, same paths, different audience) and decouples exec
  availability from share state -- fixing the phase-6 "grants-before-materials" ordering
  problem, since exec now works on unshared workspaces over the local forward.
- All remote providers converge in v1: slices get the vm instance at carve/bake; VPS
  workspaces (vultr/ovh/aws) get it from the desktop's existing outer-host provisioning pass.
  It is never installed on local outers (the user's own machine).
- **No VM-side product scripts**: clients upload scripts fresh via write-file + run per use;
  the client release is the version. The VM carries only the generic daemon (plus, later,
  pre-baked heavy latchkey dependencies).
- Consumers install pinned, sha256-verified static binaries from GitHub releases (the
  datalib-curl pattern already used by `mngr_latchkey.remote_gateway`).
- North star (not all in this effort): full web/desktop parity for imbue_cloud workspaces
  over this one channel -- latchkey provisioning + the modal-browser credential transfer, VM
  debugging (logs, supervisord status), disk/health inspection, key rotation, reboot recovery.

## Expected behavior

- The hosted web chrome discovers two exec origins per remote workspace from the share
  gateway's `/_health` service map: `owner-exec` (inner, exists today) and `vm-exec` (new).
  The presence of `vm-exec` *is* the VM-capability flag; old slices baked before this work
  simply lack the entry and the chrome hides VM-level actions.
- The browser can run a command, read a file, or write a file **on the slice VM itself** by
  sending RFC 9421-signed requests to the `vm-exec-<rand>.<domain>` origin, signed with the
  workspace Ed25519 key it already holds (generated at create, stored under the DEK).
- Every response (and the final event of a `/run` stream) carries a signature made with the
  VM's SSH host key, bound to the request; the client verifies it against the host key pinned
  in the claim response / synced workspace record. A missing or invalid signature is an
  error, never a fallback. Streamed `/run` output may be *displayed* as it arrives but is
  treated as unverified until the signed trailer checks out.
- A compromised workspace container cannot: execute on the VM, forge requests (signature),
  replay an inner envelope against the vm instance (audience `ct:` vs `vm:`), replay any
  envelope past its 60s window or nonce, or tamper with vm responses (response signature).
  It can only drop or delay traffic.
- Existing web flows (create, backups provisioning, grants read/write with CAS) behave
  exactly as today, but over the new wire format against the inner instance. The `/grants`
  endpoint remains inner-only.
- Exec no longer requires the workspace to be shared: over the desktop's local forward
  channel, both instances answer for unshared workspaces (the gate is the signature plus
  routing, not share state). Web reachability still requires a share, since routing rides
  the relay.
- Desktop-created VPS workspaces gain the vm instance on their next outer-host provisioning
  pass (the same discovery-driven pass that provisions the latchkey gateway). Slices gain it
  at bake; existing leased slices are unaffected until recreated.
- Local docker / local lima workspaces: inner instance only (as today, but the Go binary).
  Nothing is ever installed on a local outer.
- The v1 envelope is retired: the dwt Python service is deleted, the connector chrome's
  `ExecClient` speaks only the RFC profile, and the old crypto vectors are replaced by
  vectors published from the owner-exec repo. Because dev-tier chromes and workspaces must
  agree on the format, the connector deploy and the dev pool re-bake land together.

## Implementation plan

### New repo: `imbue-ai/owner-exec` (public)

- Go module, stdlib + `golang.org/x/crypto/ssh` + a pinned RFC 9421 library
  (candidate: `github.com/yaronf/httpsign`; adopt after review, else implement the profile
  in-repo). Static `CGO_ENABLED=0` builds for `x86_64-linux` and `aarch64-linux`.
- `cmd/owner-exec/main.go`: flag/TOML config load, server startup, port registration hook
  (inner role only; see dwt section).
- `internal/profile/`: the RFC 9421/9530 strict profile -- sign/verify for requests,
  responses, and stream trailers.
  - Request signature: exactly one signature (label `sig1`), `alg` pinned to `ed25519`,
    covered components exactly `@method`, `@path`, `content-digest`, `x-exec-audience`,
    `x-exec-public-key`; params `created`, `expires` (= created + 60s), `nonce`, `keyid`
    (SHA-256 SSH fingerprint), `tag="imbue-owner-exec"`. Anything else is rejected.
  - Verifier steps: exact component set match; recompute + compare `Content-Digest`
    (sha-256); `x-exec-audience` equals the instance's configured audience;
    `x-exec-public-key` parses as Ed25519 and is present in the configured
    `authorized_keys`; window check on `created`/`expires`; nonce cache claim; signature
    verifies.
  - Response signature: covered components `@status`, `content-digest`, `"@method";req`,
    `"@path";req`, `"signature";req` (binds the response to the exact request); params
    `created`, `keyid`, `tag="imbue-owner-exec-resp"`; signed with the endpoint's SSH host
    key (`/etc/ssh/ssh_host_ed25519_key`), read per use so adoption-time host-key rotation
    is picked up without restart.
  - Stream trailer: `/run` responses stream NDJSON; after the `exit` event the server emits
    one final `{"type": "signature", ...}` event whose signature covers the running SHA-256
    of all prior stream bytes plus the request's `signature` value, using the same parameter
    conventions with `tag="imbue-owner-exec-stream"` (custom construction -- 9421 signs
    complete messages, not streams -- documented in the spec).
- `internal/server/`: the endpoint surface, ported from dwt's Python service and frozen:
  - `POST /run` (streamed NDJSON stdout/stderr/exit + signed trailer; default timeout 600s,
    max 3600s, process-group kill on timeout).
  - `POST /read-file`, `POST /write-file` (atomic, mode-preserving) -- unchanged semantics.
  - `GET /grants` / `PUT /grants` with the existing revision CAS -- inner role only
    (disabled when no grants path is configured).
  - `GET /_alive` (unauthenticated loopback/bridge liveness; reports version + role).
  - CORS: echo the configured chrome origin exactly (credentialed), now allowing the
    `Signature`, `Signature-Input`, `Content-Digest`, `X-Exec-Audience`,
    `X-Exec-Public-Key` headers.
  - Hardening: request-body size cap, bounded concurrent `/run` commands, window-pruned
    nonce cache, no body logging, systemd/supervisord restart + `MemoryMax` backstop.
- Config (TOML file + flag overrides): `role` (`inner`|`vm`), `listen_host`, `listen_port`,
  `authorized_keys_path`, `audience`, `host_key_path`, `grants_path` (empty disables),
  `default_cwd`, `share_env_path` (chrome origin source; empty disables CORS), limits.
- `spec/`: the profile spec document (covered components, tags, verifier requirements,
  the trailer construction, threat model incl. the audience-separation argument) and the
  deferred-latchkey groundwork sketch (below).
- `vectors/`: generated JSON test vectors -- valid and invalid requests, responses, and
  trailers (tampered digest, stale created, replayed nonce, wrong audience, unauthorized
  key, non-Ed25519 key, stripped response signature). Consumed by the Python and TS clients.
- Release workflow: per-arch tarballs + `.sha256` files on GitHub releases.

### default-workspace-template (dwt)

- `system/Dockerfile`: fetch the pinned owner-exec release (+ sha256 verify) into
  `/usr/local/bin/owner-exec`; version pin constant in the Dockerfile.
- `system/supervisord.conf`: `[program:owner-exec]` command becomes the binary (role
  `inner`, listen `127.0.0.1:8793`, audience `ct:<host-id>` resolved at start from the mngr
  host record / env, grants path `data/.secrets/share_grants.toml`, host key
  `/etc/ssh/ssh_host_ed25519_key`, chrome-origin from `data/.secrets/share.env`). Port
  registration (`forward_port.py --name owner-exec`) moves into the launcher wrapper or a
  companion one-shot, preserving today's best-effort semantics.
- New `[program:vm-exec-register]` one-shot (autorestart=false): resolve the container's
  default gateway from `ip route`, probe `http://<gw>:8794/_alive`, and when it answers,
  register `vm-exec` via `forward_port.py --name vm-exec --url http://<gw>:8794`. On local
  workspaces the probe fails and nothing registers.
- Delete `system/services/owner_exec/` (Python service, its tests and ratchets); the repo
  keeps only config/launcher glue. Caddy/gateway need no changes (apps.toml rows already
  carry arbitrary backend hosts; `/_health` lists `vm-exec` automatically once registered).

### Monorepo: `libs/imbue_common`

- New `owner_exec_client.py` (name TBD at implementation): the Python ExecClient twin --
  profile signing via pinned `http-message-signatures`, response + trailer verification
  against a pinned host key, streaming `/run` consumption, grants CAS helpers. Raises on
  missing/invalid response signatures. Vector-driven unit tests (vendored copy of the
  owner-exec vectors).

### Monorepo: `apps/remote_service_connector/frontend_web`

- `src/exec.ts` + `src/crypto/ed25519.ts`: replace v1 envelope construction with the RFC
  profile (evaluate `http-message-signatures` / Cloudflare web-bot-auth libs for browser
  fit; else a minimal profile implementation over the existing WebCrypto Ed25519 code).
  Add response/trailer verification against the pinned host keys from claim/record.
- Replace `scripts/generate_crypto_vectors.py`'s exec-envelope portion with the vendored
  owner-exec vectors (the secret-wrapping vectors stay).
- Discover and use the `vm-exec` origin from `/_health` (mirrors `execOriginFromHealth`).

### Monorepo: `libs/mngr_imbue_cloud`

- Slice carve/bake (`slices/lima_slice.py` provision scripts + `bake/pool_bake.py`):
  install the pinned binary + sha256 verify + version stamp; write
  `/etc/owner-exec/config.toml` (role `vm`, audience `vm:<host-id>` -- written post-bake
  once `mngr create` has produced the host id -- bridge listen address, share-env path on
  the host volume for CORS); install + enable a systemd unit (`Restart=always`,
  `MemoryMax` backstop). Version pin constant beside the existing install pins.

### Monorepo: `libs/mngr_latchkey`

- `remote_gateway.py` / `discovery.py`: the existing VPS outer-host provisioning pass
  additionally installs/updates the owner-exec vm instance (pinned fetch, config write with
  `vm:<host-id>`, systemd unit) before the gateway steps -- pragmatic piggyback; revisit
  when desktop provisioning itself converges onto vm-exec. `is_local` guard unchanged.

### Monorepo: misc

- `.claude/skills/bump-owner-exec/`: bump skill documenting every pin site (dwt Dockerfile,
  mngr_imbue_cloud carve, mngr_latchkey VPS install, vendored vector copies here and in dwt).
- Docs: security-boundaries notes (VM trust boundary, audience separation, response
  signing, VM-owned-state principle); update the web-client exec docs; changelog entries per
  touched project.

### Deferred groundwork: latchkey on web (captured for future implementors, not built now)

- Bake-time: pre-install node, the pinned latchkey CLI, and supervisor in the slice image so
  runtime provisioning never apt/npm-installs.
- Browser-side provisioning over vm-exec (uploading scripts per use, never baked): write
  tmpfs secrets (`/run/mngr-latchkey`: encryption key, derived listen password), write
  permissions/config/credentials, start supervisord programs -- mirroring
  `remote_gateway.py`'s steps minus the install.
- Credential acquisition: a **connector endpoint mints a temporary Modal container** running
  a browser streamed into an iframe (dwt's browser-streaming approach); the user runs
  latchkey's browser auth there; extracted credentials are handed back to the user's browser,
  **re-encrypted under the account DEK**, and transferred into the VM's latchkey store via
  vm-exec -- credentials never enter the workspace container and only transit the Modal
  container ephemerally.
- Web-only workspaces omit the desktop-forwarding extension (no desktop to forward to); a
  desktop that later discovers the workspace adds it. Permission-request review on web can
  initially drive the gateway's loopback extensions through vm-exec `/run`.

## Implementation phases

1. **owner-exec repo v0.1**: profile spec + vectors + daemon with both roles, unit tests,
   release pipeline. Exit: a signed round trip (incl. response + trailer verification)
   against the binary, driven by vector-based test clients.
2. **Clients**: `imbue_common` Python client (vector-validated); connector `ExecClient`
   rewritten to the profile (not yet deployed). Exit: both clients pass the shared vectors,
   Python client round-trips against a locally spawned binary.
3. **dwt swap**: Dockerfile fetch, supervisord changes, `vm-exec-register` one-shot, Python
   service deleted. Deployed together with the connector chrome to the dev tier (formats
   must flip in lockstep); dev pool re-baked. Exit: web create/backups/grants loop works on
   dev over the new profile.
4. **Slices**: carve/bake installs the vm instance + config; re-bake the dev pool. Exit
   (manual verification): browser signs a command on a fresh dev slice VM via
   `vm-exec-<rand>.<domain>`, output streams, trailer verifies against the claim's VM host
   key; a request signed with the inner (`ct:`) audience is rejected by the vm instance.
5. **VPS**: piggyback install in the latchkey provisioning pass. Exit (manual verification):
   a vultr/ovh workspace exposes `vm-exec`, desktop-local-forward exec works unshared, and
   the latchkey gateway provisioning still succeeds after it.
6. **Cleanup and convergence**: retire v1 vectors/generator, docs + security-boundary notes,
   bump-owner-exec skill, `ssh_utils.py` comment update, changelog entries; list follow-ups
   (desktop provisioning + grants writes onto this channel, latchkey-on-web, key rotation on
   web).

## Testing strategy

- **owner-exec repo (Go)**: unit tests for the profile (every verifier step, each invalid
  vector), the nonce cache, streaming (interleaving, timeout kill, trailer over exact
  bytes), grants CAS, atomic writes, limits. Vector generation is a build step; RFC 9421's
  published examples validate the canonicalization where the profile overlaps them.
- **dwt integration**: container tests exercise the real binary end to end -- signed run /
  read / write / grants CAS, tamper cases (body vs digest, wrong audience, replayed nonce,
  stale created, stripped response signature), CORS preflight, `vm-exec-register` no-op on
  a local workspace.
- **Monorepo**: `imbue_common` client unit tests against vendored vectors; connector
  frontend vitest against the same vectors; a drift check that vendored vectors match the
  pinned owner-exec version.
- **Deployment tests**: deliberately deferred to the latchkey effort (per decision); the
  live-slice and VPS paths in phases 4-5 are verified manually (browser + tmux, not
  crystallized into pytest).
- **Edge cases to cover explicitly**: cross-instance replay (`ct:` envelope at the vm
  instance and vice versa); nonce replay across instance restart (accepted window --
  document); unshared-workspace exec over local forward; missing `Content-Digest`; GET with
  empty body digest; oversize body; concurrent `/run` at the cap; host-key rotation between
  request and verification (client re-reads pins); container down => vm-exec unreachable
  (routing dependency, accepted).

## Open questions

- Final TS library choice (dhensby `http-message-signatures` vs Cloudflare web-bot-auth vs
  minimal in-repo profile implementation) pending a browser-compatibility check against the
  chrome's WebCrypto usage.
- Go library approval: `yaronf/httpsign` looks suitable; confirm during review that the
  strict-profile constraints (single signature, fixed components, ed25519-only) can be
  enforced on top of it cleanly.
- Nonce-cache persistence across daemon restarts (a restart re-opens the <=60s replay
  window): accept and document, or persist recent nonces to tmpfs. Leaning accept.
- Whether the slice image pre-installs the latchkey heavy deps now (while we're touching the
  bake) or with the latchkey effort. Leaning with the latchkey effort to keep this one small.
- vm-exec is unreachable when the container (and its share/forward stack) is down; fine for
  configuration flows, but reboot-recovery parity may eventually want a container-independent
  path (e.g. a VM-side relay claim). Follow-up, not v1.
- Release tooling for the new repo (goreleaser vs plain make + workflow) -- implementer's
  choice.
