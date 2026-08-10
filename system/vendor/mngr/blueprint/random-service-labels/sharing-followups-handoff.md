# Sharing follow-ups — handoff

Leftover issues from the `mngr/test-forwarding` testing pass of the self-hosted
sharing redesign (random per-service labels). The core feature works end to end
on the `dev-josh-1` env; these are the known-unfixed items, in priority order.
Paths are in the mngr repo unless noted `[dwt]` (default-workspace-template,
checked out at `.external_worktrees/default-workspace-template`).

Repos/branches: mngr `mngr/test-forwarding` (base `josh/merge-forwarding`,
`main` merged in); dwt `mngr/test-forwarding` (base `origin/mngr/merge-forwarding`).
Design context: `blueprint/random-service-labels/plan-random-service-labels.md`
and `blueprint/sharing-redesign/plan-sharing-redesign.md`.

---

## 1. Broker OAuth on the accounts sign-in page (biggest functional gap)

**Symptom.** A share visitor is bounced to the accounts broker sign-in page,
which offers ONLY email + password. An account created via Google/GitHub OAuth
(e.g. most real imbue accounts) has no password and cannot sign in, so it can
never open a share. "Create account" with that email is refused
(`ACCOUNT_EXISTS_WITH_OTHER_METHOD`), so there is no self-rescue.

**Where.** `apps/remote_service_connector/imbue/remote_service_connector/app.py`:
- `_BROKER_LOGIN_PAGE_TEMPLATE` (~line 3424): the hand-rolled login form; posts
  email/password/mode to `/share/session`. This is where OAuth buttons must be added.
- `broker_login_page` GET `/share/login` (~3552); `broker_create_session` POST
  `/share/session` (~3585); `broker_authorize` GET `/share/authorize` (~3636).
  The broker session is the `imbue_sso_session` cookie holding the SuperTokens
  access token (`_broker_session_user`, ~3520).
- **Existing OAuth machinery to reuse**: `POST /auth/oauth/authorize` (~7048,
  returns the provider authorize URL) and `POST /auth/oauth/callback` (~7072,
  exchanges the provider's params for a SuperTokens session). Today these serve
  the desktop CLI's local-callback flow, not a browser redirect flow.
  `_build_oauth_providers` (~7246) builds the provider configs from the
  `GOOGLE_*` / `GITHUB_*` env (the `supertokens-<env>` secret).

**Recommended fix.** Add a browser OAuth redirect flow to the broker:
- Login page gains "Continue with Google / GitHub" links to a new
  `GET /share/oauth/<provider>?next=&callback_origin=&state=` that computes the
  provider authorize URL (reuse `auth_oauth_authorize`'s logic) with the
  broker's OWN redirect URI (`<connector>/share/oauth/<provider>/callback`) and
  the share params carried through `state` (signed or stashed).
- `GET /share/oauth/<provider>/callback` exchanges the code (reuse
  `auth_oauth_callback`), sets the `imbue_sso_session` cookie exactly as
  `broker_create_session` does, and 302s back into `/share/authorize` with the
  original `machine_domain`/`next`/`callback_origin`/`state`.
- Redirect URIs must be registered on the Google/GitHub OAuth clients for each
  tier's accounts host (dev: the connector URL; prod: `accounts.imbue.com`).
- Keep the email/password form; OAuth is additive.

**Watch out.** The share params (`machine_domain`, `next`, `callback_origin`,
`state`) must survive the provider round-trip — carry them in the OAuth `state`
param and re-validate `callback_origin`/`next` under `machine_domain` on return
(the validators `_is_origin_under_domain` / `_is_url_under_domain` already exist).
Cross-site-POST CSRF guard (`_is_cross_site_form_post`) is for the password form;
the OAuth GET flow needs its own `state`-nonce check.

**Test.** Seeded email/password visitor still works; a Google account reaches the
share after the provider round-trip; `state` mismatch is rejected.

---

## 2. Grants-file write race (corruption)

**Symptom.** Two concurrent grant edits (Share pane open from both the titlebar
and the workspace list, or two windows) can corrupt `share_grants.toml` inside
the workspace. Manifested this session as a transient "Malformed grants document"
warning (see also #3, which turns this into data loss).

**Where.**
- `apps/minds/imbue/minds/desktop_client/share_materials_injection.py`
  `_write_file_via_exec` (~line 83): writes via a FIXED tmp name
  (`{relative_path}.tmp`) then `mv`. The `mv` is atomic for readers, but two
  writers share the one tmp path and clobber each other. `inject_share_grants_into_agent`
  (~98) and `inject_share_materials_into_agent` (~103) both use it.
- `apps/minds/imbue/minds/desktop_client/api_v1.py` `_handle_machine_sharing_put`
  (~2690): no per-host lock, so two PUTs run `enable_sharing` →
  `inject_share_grants_into_agent` fully concurrently.
- The desktop-side JS serializes writes only per-pane
  (`static/workspace_options.js` `writeChain`, ~558), which does not cover two
  panes/windows.

**Recommended fix.** (a) In `_write_file_via_exec`, use a unique tmp name
(`mktemp` in the same dir inside the exec'd shell) so concurrent writers never
share it; (b) add a per-host (or per-agent) lock in the desktop backend around
the read-modify-write in the machine-sharing PUT so grant edits for one machine
serialize regardless of pane/window.

**Test.** Fire two concurrent grant PUTs for one host; the resulting file always
parses and reflects one of the two writes (last-writer-wins), never a mix.

---

## 3. Malformed grants read presents as empty → whole-doc save wipes grants (DATA LOSS)

**Symptom.** A transient malformed/failed read of the grants file makes the Share
pane render as "no grants," and because every save REPLACES the whole document,
the next edit permanently erases all real grants. Fail-open in the worst way.

**Where.** `apps/minds/imbue/minds/desktop_client/sharing_handler.py`:
- `_parse_grants_toml` (~257): on `tomllib.TOMLDecodeError` returns
  `({"emails": [], "email_domains": []}, {})` (empty) with only a `logger.warning`.
- `read_share_grants_from_agent` (share_materials_injection.py ~140) already
  distinguishes a failed exec (raises) from an absent file (None) — a prior fix
  ("report unlanded grants reads as unknown instead of empty") covered the
  FAILED-read case, but a MALFORMED parse still collapses to empty here.
- `get_sharing` (~275) builds the document the Share pane renders; the PUT then
  replaces the whole grants document.

**Recommended fix.** Treat a malformed read the same as "unknown," not "empty":
propagate a distinct state so the Share pane shows an error and BLOCKS edits
until a clean read lands, rather than rendering empty and letting a save
overwrite. (Pairs naturally with #2 — a unique tmp name makes malformed reads
rare, and fail-closed-on-malformed makes the rare one non-destructive.)

**Test.** With a deliberately malformed `share_grants.toml`, `get_sharing`/the
readiness of the pane reports unknown (not empty), and a PUT is refused rather
than wiping grants.

---

## 4. UX: enabling with un-added text in the email box silently drops it

**Symptom.** The user types an email in the add-email input but does not click
"Add", then enables/saves; the typed address is silently dropped (this is how a
share went out to the wrong/empty grantee earlier this session).

**Where.** `apps/minds/imbue/minds/desktop_client/static/workspace_options.js`:
- Add-email input is `ws-share-new-email`; the add handler reads
  `input.value.trim()` and clears it (~780-786). On enable/save the pending
  (un-added) input text is ignored.
- Template: `templates/WorkspaceShareSection.jinja` (the `ws-share-new-email`
  input + Add button, ~near the emails list `ws-share-emails` at line 106).

**Recommended fix (user's request).** On enable/save, if the add-email input has
non-empty text after trimming, block with an inline error: "Either click 'Add' to
add <text>, or clear the box." Do not auto-add (ambiguous) and do not silently
drop.

**Test.** Enable with residual text → inline error, no network write; clear or
Add → proceeds.

---

## 5. UX: make the "Add" button obvious when the input has text

**Symptom.** The Add button is visually quiet, so users type and forget to click
it (root of #4).

**Where.** Same files as #4: `static/workspace_options.js` (toggle a class on
input) + `templates/WorkspaceShareSection.jinja` (the Add button) + the Tailwind
classes (the pane is Tailwind; see `apps/minds/.../static/app.css` build).

**Recommended fix (user's request).** When `ws-share-new-email` is non-empty,
switch the Add button to a prominent/accent style (and back when empty), so it
reads as the obvious next action. Pure front-end.

---

## 8. Scanner residual — real client IPs (do) + relay rate tiers (sanity)

Context: the random-labels feature already SOLVES the core CT-scanner problem —
scanners only learn the bare `<ws-domain>` from Certificate Transparency, which
no longer routes (only explicit `<label>.<domain>` claims are served). These two
items are hardening/observability, not the fix.

**8a. Real client IPs at the gateway (worth doing).**
Today caddy (in the workspace) sees every connection as coming from frpc on
127.0.0.1 — it cannot tell a scanner from a visitor, so per-IP rate limiting and
telemetry are impossible in-workspace.
- Enable frp PROXY protocol: set `transport.proxyProtocolVersion = "v2"` on the
  share proxy in `[dwt] system/services/share_gateway/src/share_gateway/frpc_config.py`
  (`render_frpc_toml`), and have caddy consume it via a `proxy_protocol` listener
  wrapper + `trusted_proxies` in the rendered Caddyfile (`[dwt] .../caddyfile.py`).
  frps must also pass it through (it does for `https`/tcp proxies; verify the
  relay's `frps.toml` in `apps/share_relay/imbue/share_relay/config_render.py`).
- Then the gateway (`[dwt] .../server.py` `/_auth/verify`) can log/act on the
  real client IP (via caddy's forwarded header).
- Test: a request from a known external IP surfaces that IP at the gateway, not
  127.0.0.1.

**8b. Relay rate tiers (sanity — mostly already present).**
The relay ALREADY has tier-2 abuse guards: nftables per-source-IP
new-connection-rate + concurrent-connection caps on the vhost port, rendered by
`apps/share_relay/imbue/share_relay/config_render.py` (`RelayConfiguration`
fields `max_new_connections_per_second_per_ip` / `_burst` / connlimit) and the
`:80 -> https` redirector. For "sanity if easy": confirm those nftables limits
are actually loaded on the running dev relay (`ssh debian@<relay> sudo nft list ruleset`)
and tune the caps if the scanner trickle warrants. A per-SNI or auth-informed
tier (starve unknown IPs, normal service for broker-authorized IPs) needs a
connector→relay feed and is deliberately OUT of scope here — the label design
already denies scanners at the relay by SNI, so this is optional.

---

## Env / test notes (for whoever picks this up)

- Dev env `dev-josh-1`: connector `https://minds-dev-dev-josh-1--rsc-dev-api.modal.run`
  (redeployed with the new broker/NewProxy code). Relay `dev1` at 15.204.31.248
  (`share-relay-dev-josh-1-dev1`, Debian 13). Vault sharing secret at
  `secrets/minds/dev/sharing`; relay SSH key + OVH project id at
  `secrets/minds/dev/{relay-ssh,ovh}` (see `apps/minds/docs/vault-setup.md`).
- Seeded email/password visitor for share testing: `josh_staging+visitor@imbue.com`
  (creds in `~/.minds-dev-josh-1/share-visitor-account.txt`).
- Hard cutover: existing workspaces predate the label registry; create a FRESH
  workspace to exercise the feature. `just minds-start` rsyncs live mngr into the
  dwt vendored copy; the desktop-client (apps/minds) Python/JS/templates are
  served straight from the checkout, so a plain app restart picks them up
  (workspace-side dwt changes need a fresh workspace or `just propagate-changes`).
- The reviewer stop-hook was disabled this session
  (`/imbue-code-guardian:reviewer-enable` to restore).
