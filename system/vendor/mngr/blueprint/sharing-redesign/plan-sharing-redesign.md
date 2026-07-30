# Plan: Sharing redesign — self-hosted, end-to-end-encrypted, service-per-origin

## Overview

- Replace Cloudflare tunnel/Access sharing entirely with self-hosted infrastructure: an SNI-passthrough relay (frps) that never terminates TLS, TLS termination *inside* the workspace container, and an in-container auth gateway. The relay sees only SNI hostnames and ciphertext — true end-to-end encryption between browser and workspace.
- Simultaneously redo local forwarding as service-per-origin (informed by, not based on, external PRs mngr#2597 / default-workspace-template#342): every registered service owns a real browser origin, deleting the `/service/<name>/` proxy stack (service-worker bootstrap, fetch-prefixing SW, `<base>` injection, HTML path rewriting, WebSocket shim, cookie-Path rewriting) from `system_interface`.
- One hostname grammar everywhere, using full untruncated ids (`host-<32hex>`, full SuperTokens user id):
  - Local: `https://<service>.<host-id>.localhost:8421/`; bare `<host-id>.localhost:8421` is the shell.
  - Shared: `https://<service>.<host-id>.<user-id>.<region>.imbueminds.com/`; bare `<host-id>.<user-id>.<region>.imbueminds.com` is the shell. Deeper labels route to the owning service.
  - The shell (`system_interface`) never appears as a hostname label, so its underscore name is a non-issue; only non-shell services need DNS-safe names.
- Regions: `us1` (OVH Hillsboro) and `us2` (OVH Vint Hill) now. Region codes are pure config rows (`suffix -> relay endpoint`); dev envs use their dev suffix (e.g. `dev-josh-1.minds-dev.com`) in the region slot. Internal note documents future expansion codes (us3+, eu, sa, ap, au, me, af) — not registered now.
- Public Suffix List: single wildcard entries `*.imbueminds.com`, `*.minds-staging.com`, `*.minds-dev.com`. Makes each `<user-id>.<region>.<domain>` its own registrable domain: per-user site isolation (no cross-tenant cookie planting, SameSite actually protects across tenants) while one user's services stay same-site (iframes + one domain cookie work, including Safari on shares).
- Auth requires an imbue account (the signup funnel): a hosted SuperTokens login/signup UI at `accounts.imbue.com` (Modal custom domain, keep-warm), global SSO cookie `Domain=imbue.com` (Secure/HttpOnly/Lax). Safe because all user content lives on imbueminds.com; policy: imbue.com subdomains are first-party only, user content never gets an imbue.com hostname.
- Authorization lives in the workspace: a grants TOML file checked on every request by the gateway. Workspace-level grant implies all services; per-service grants; email-domain grants. Revocation is instant (file edit).
- Certs: workspace generates key + CSR (private key never leaves the container); the connector — sole custodian of the Cloudflare DNS API credential — completes ACME DNS-01 and returns a SAN-only cert (`<ws-domain>` + `*.<ws-domain>`; names exceed the 64-char CN limit). Ordered multi-CA config (ZeroSSL/GTS via EAB preferred while LE's per-domain bucket is constrained pre-PSL; LE-first after).
- Cloudflare is dropped for tunnels/Access/KV but kept as a plain DNS host (gray-cloud records, zone-scoped API token). CF account cleanup is follow-up work (preserves rollback).
- Cost: ~$15-50/month total relay spend (OVH Public Cloud instances, unmetered bandwidth, free OVH anti-DDoS), versus per-tunnel Cloudflare pricing.
- Deferred by design, with clean seams: public (unauthenticated) services; wake-on-visit for paused hosts (v1: host must be running; unmatched-SNI path kept clean); bandwidth quotas/metering (tier 3); deep sub-origin cert SANs (openvscode webviews — reissue hook designed, unbuilt); machine/service tokens (no consumers today); moving cert issuance to workspace-creation time (post-PSL follow-up).
- v1 is imbue-only, but relay endpoint, domains, and broker URLs are configuration, not constants — a self-hoster could point at their own frps/domain/IdP later.

## Expected behavior

### Local (minds desktop)
- The workspace shell loads at `https://<host-id>.localhost:8421/` via `mngr forward` (unchanged h2/TLS mode, per-SNI cert minting for nested names).
- Each panel is an iframe at `https://<service>.<host-id>.localhost:8421/` — a real origin. Root-absolute URLs, `new WebSocket("/ws")`, `Set-Cookie: Path=/`, and service workers work unmodified. Unregistered-but-plausible service labels get the auto-retrying loading page.
- Login happens once per workspace via the existing `/goto/<host-id>/` bridge; the session cookie is scoped `Domain=<host-id>.localhost` and covers every service subtree.
- Layouts persist `serviceName`, never origins; URLs are re-derived at render time, so saved layouts are portable across hosts and shares.
- Supported locally: Electron/Chromium/Firefox. Safari-local is documented as unsupported (WebKit treats each `x.localhost` as its own site); remote shares work in Safari.

### Sharing (Alice shares, Bob visits)
- Alice clicks Share in machine settings, adds emails/domains. First share of a workspace shows a "provisioning" state (~30-90s: relay token, key+CSR, ACME cert, tunnel up); re-shares are fast (cert kept on disk). The modal then shows `https://<host-id>.<user-id>.<region>.imbueminds.com/`.
- Bob opens the link: DNS wildcard -> relay -> SNI match -> byte-splice into Alice's tunnel -> TLS handshake terminates *in the workspace*. No session: gateway 302s to `accounts.imbue.com` (login/signup if needed), broker mints a 60s audience-bound signed token, redirects back; gateway verifies (JWKS), checks grants, sets one workspace-domain cookie, lands Bob on the shell. Two redirects, silent if already logged in.
- Every request: cookie verified, email re-checked against grants (instant revocation), cookie stripped before forwarding, Host label routed to the local port from `apps.toml`. Panels are same-site iframes — cookies flow with plain Lax in Chromium, Firefox, and Safari.
- Per-service grants: `services.web = ["carol@..."]` admits Carol to `web.<ws-domain>` only; the shell and siblings 403 for her. Workspace-level grants imply all services (no way around that). Standalone per-service share links are just the service origin plus a per-service grant.
- Origin enforcement at the gateway: WebSocket upgrades require `Origin` ∈ this workspace's origins; non-GET requests with a present-but-foreign Origin are rejected; GETs exempt.
- A service registered while shared is reachable immediately (wildcard cert + wildcard vhost + apps.toml routing) — no control-plane action.
- Unshare: grants cleared, gateway/frpc/Caddy stop, tunnel drops (live viewers cut), cert/key stay on disk for fast re-share; connector marks the share inactive and deletes the relay token.
- Paused/stopped host: tunnel is down; visitors get a connection error (v1). Existing idle rules unchanged; sharing does not keep a host awake.
- Quota: 50 concurrent shared machines per user, enforced at share-enable. No per-service cap.
- Existing Cloudflare shares stop working when the release ships (per-env sequencing; no dual-stack); users re-share. CF resource cleanup is follow-up.
- HTTP on :80 at the relay: dumb same-host 301 to https.

### Failure modes
- Relay compromised: sees SNI + traffic volume only; misrouted connections fail TLS.
- Connector down: existing tunnels and sessions keep working; new logins and tunnel (re)connects fail until it returns (fail-closed; frpc retry self-heals).
- Workspace compromised: blast radius is its own subtree (its key, its grants, its traffic).
- Grants file malformed: gateway fails closed (403 everything, owner sees error in status).

## Implementation plan

### New project: `apps/share_relay` (mngr monorepo)
- `frps.toml` template: `bindPort` (tunnel control), `vhostHTTPSPort = 443` (SNI passthrough), server-plugin block subscribing to `Login` + `NewProxy` ops pointed at the connector, dashboard/metrics off or loopback-only.
- Port-80 redirector: minimal same-host 301-to-https listener (systemd unit or tiny Go/Python binary in the image).
- `nftables.conf`: per-source-IP new-connection rate limit (20/s burst 40), per-IP connlimit (100), global conntrack ceiling.
- Cloud-init / image build for OVH Public Cloud (Debian base, pinned frp release binary + checksum, healthcheck endpoint).
- Healthcheck: trivial HTTP endpoint (frps liveness + tunnel count) on the infra hostname; no monitoring wiring in v1.
- `justfile` recipes: provision/deploy/destroy relay per env+region via OVH Public Cloud (OpenStack) API; prod 2 (us1, us2), staging 2, dev 1 minimal instance.
- Infra DNS names under the product domain: `relay-us1.infra.imbue.com` (+ staging/dev twins); user-content wildcards `*.us1.imbueminds.com` -> relay IP etc. (static, created once per env by an ops recipe).
- Changelog entry.

### `apps/remote_service_connector`
- Delete the Cloudflare stack: `ForwardingCtx` CF operations (tunnels, per-hostname CNAMEs, ingress PUTs, Access apps, service tokens, Workers KV policy storage) and the `/tunnels/**`, `/sharing/enable` endpoints.
- Keep and reshape: SuperTokens auth, entitlements/quotas (new: 50 concurrent shared machines/user), pool/LiteLLM/R2 (untouched).
- New Neon Postgres tables: `shares` (host_id, user_id, region, domain, status, created), `relay_tokens` (opaque token hash -> share), `acme_accounts` (per CA, incl. EAB), `issued_certs` (cert PEM, SANs, expiry; no private keys — CSR flow).
- New endpoints:
  - `POST /api/v1/shares` (user JWT): quota check, region from host's DC, create share row + opaque relay token; returns `{workspace_domain, relay_endpoint, relay_token}`.
  - `DELETE /api/v1/shares/<host_id>` (user JWT): deactivate, delete relay token.
  - `POST /api/v1/shares/cert` (relay-token auth): accepts a CSR, drives ACME DNS-01 against the ordered CA list (lego-equivalent Python ACME client; CF DNS API zone-scoped token for TXT records), returns cert chain. Used for both first issuance and renewal.
  - `GET /api/v1/shares/<host_id>/status` (user JWT): tunnel connected? cert expiry? — backs the sharing modal.
  - `POST /frps/auth` (shared-secret from relay): frps server-plugin handler for `Login` (validate relay token, share active) and `NewProxy` (claimed `customDomains` must equal the token's workspace domain + wildcard). Keep-warm.
- Broker (same app, served at `accounts.imbue.com` via Modal custom domain, keep-warm):
  - Hosted SuperTokens login/signup UI (prebuilt UI recipe), open self-serve signup; SSO cookie `Domain=imbue.com; Secure; HttpOnly; SameSite=Lax` (staging: `accounts.imbue-staging.com` mirrors this on imbue-staging.com; dev: host-only cookies on per-dev accounts hostnames under imbue-dev.com).
  - `GET /share/authorize?machine_domain=&next=&state=`: requires session (else login UI); validates `machine_domain` against an active share; mints 60s RS256 JWT `{sub, email, aud: machine_domain, jti, nonce}`; 302 to `https://<machine_domain>/_auth/callback?token=&state=&next=`.
  - JWKS published at a well-known URL for gateways.
- ACME CA config: ordered list with per-CA directory URL + optional EAB, in env deploy config.

### `default-workspace-template` (separate repo; one PR referencing this spec)
- Delete `system/apps/system_interface/imbue/system_interface/proxy.py`, `service_dispatcher.py`, their tests, `register_service_routes` wiring; adjust the SPA catch-all.
- Frontend: `deriveServiceOrigin(serviceName)` = one rule — prefix the service label onto the current workspace domain (`location.host`, minus any leading service label). Update `DockviewWorkspace.ts` (service refs, terminal/browser URLs, layout restore re-derivation), `IframePanel.ts` comments/sandbox, `CreateBrowserModal`; add a same-origin `/api/browsers` backend passthrough for the browser-fleet API (sibling origins are same-site but not same-origin).
- `system/scripts/layout.py` / `layout_ops.py`: speak service coordinates; parse service names from both hostname shapes on restore.
- `system/scripts/forward_port.py`: validate DNS-safe names (lowercase alnum + single hyphens; underscores tolerated for legacy `system_interface`; reject `agent-`/`host-` prefixes and reserved names) + tests.
- New `system/services/share_gateway/` (the forward_auth service, Python, sync):
  - `_auth/verify` endpoint for Caddy `forward_auth`: cookie verify -> grants check (workspace + per-service + email domains) -> Origin policy (WS upgrades: Origin required and ∈ workspace origins; non-GET: reject foreign Origin; GET exempt) -> allow/deny; strips the session cookie via Caddy header directives.
  - `_auth/callback`: state-nonce check, JWT verify against broker JWKS (cached; refresh on unknown kid), `aud`/`exp`/`jti` (single-use cache) checks, grants check, set signed session cookie (`Domain=<ws-domain>; Secure; HttpOnly; SameSite=Lax`, 24h; signing secret persisted in `data/.secrets/`).
  - Unauthenticated HTML navigations 302 to the broker with `machine_domain`/`next`/`state`; non-HTML get 403. Unknown service label: auto-retrying loading page. Malformed grants: fail closed.
  - Key + CSR generation at first share; `POST /shares/cert` with relay token; daily renewal check (<30 days -> new CSR, same key, reload).
  - Renders the Caddyfile from `apps.toml` (bare origin -> `system_interface` port; `<label>.*` and deeper -> that service's port) and reloads Caddy via admin API on changes (same watch pattern as `AgentManager`).
- Supervisord: remove `cloudflared` + the `cloudflare_tunnel` service; add `share-gateway`, `caddy`, `frpc` programs, all gated on share materials (`data/.secrets/share_*` present), stopped on unshare. Image gains pinned `caddy` and `frpc` binaries.
- Caddy config: wildcard site, `tls <cert> <key>` from files, `forward_auth` to share_gateway, static per-service reverse_proxy matchers (h2 front, h1 to backends, WS native).
- Skills/docs: rewrite build-app and friends for own-origin apps (no prefix guidance); update workspace-internals docs.
- Changelog entries per project.

### `libs/mngr_forward` (local per-origin, redone)
- `primitives.py`: host pattern accepts optional service labels before the full host id: `[<labels>.]<host-id>.localhost(:port)`; last label before the host id selects the service, deeper labels are its sub-origin space; `ServiceLabel` validation.
- `resolver.py`: `resolve(host_id, service_name | None)`; bare origin -> shell service (configured strategy); named service -> that host's primary agent's registered service map. Internally maps host id -> primary agent (observe stream already carries host ids; `--agent-include is_primary` unchanged).
- `server.py`: route by parsed host; `/goto/<host-id>/` bridge carries the service label chain and sets the cookie with `Domain=<host-id>.localhost`; loading page for unregistered-but-plausible labels; strip-before-forward unchanged.
- `tls.py`: per-SNI cert minting for nested `.localhost` names (static SANs can't cover unknown ids/depths), cached, signed by the ephemeral key.
- Tests for all of the above.

### `apps/minds`
- `desktop_client/sharing_handler.py`: rewrite — enable = connector `POST /shares` -> inject relay token + grants + gateway config via `mngr exec` -> poll readiness (TLS probe of the real hostname + connector status); disable = clear grants + stop services + `DELETE /shares`. No CF fan-out, no base-path splitting.
- `desktop_client/api_v1.py`: new `GET/PUT/DELETE /api/v1/machines/<host_id>/sharing` carrying the grants document `{workspace: {emails, email_domains}, services: {<name>: {emails, email_domains}}}` + `/readiness`; old per-service workspace sharing endpoints removed. "machine" naming for new surface only.
- Sharing UI (settings + modal): workspace master list, per-service lists, email-domain entries, per-service standalone links, provisioning progress, live status (connector status endpoint).
- `imbue_cloud_cli.py` / `libs/mngr_imbue_cloud`: replace tunnel CLI/client with share endpoints (`shares create/delete/status`); delete CF-specific client code and the service-token surface.
- Electron: `surface-routing.js` / `chrome.js` regexes accept `[<labels>.]<host-id>.localhost`; persisted window URLs become `/goto/<host-id>/`; window-open/external classification unchanged (`.localhost` suffix rule holds).
- Probes: `agent_creator.py` workspace probe Host header -> `<host-id>.localhost`; `system_interface_health` unchanged in substance; `e2e_workspace_runner.py` terminal iframe selector -> `src^="https://terminal."`.
- `forward_cli.py`: service map consumption unchanged; host-id keyed forwarding.
- Config: region map (`region -> relay endpoint`), accounts/broker URLs, per-env domains in `config/envs/*`.
- Changelog entries.

### External / ops checklist (long lead times — start immediately)
- PSL PR: `*.imbueminds.com`, `*.minds-staging.com`, `*.minds-dev.com` + `_psl` TXT records; internal note for future region codes.
- Let's Encrypt rate-limit increase request; ZeroSSL + GTS EAB registrations.
- Register `imbue-staging.com` and `imbue-dev.com`; Modal custom domains for accounts pages.
- Cloudflare zone-scoped DNS API token for the connector; static wildcard + infra DNS records per env.

## Implementation phases

- **Phase 0 — spikes + external kickoffs.** Verify frp: SNI-passthrough vhost, wildcard `customDomains`, `Login`/`NewProxy` plugin semantics (fallbacks: sish or a small custom SNI router). Verify Caddy: `forward_auth` + admin-API reload + h2/WS proxying. Verify SuperTokens: hosted UI + OAuth-ish handoff + Modal custom domain. File PSL PR, LE increase, EAB registrations, domain registrations. Nothing user-facing changes.
- **Phase 1 — local service-per-origin cutover.** mngr_forward host-id + label routing + per-SNI TLS; template proxy deletion + origin derivation + forward_port validation; minds Electron/probe/selector updates. Result: local system fully per-origin. (CF workspace shares degrade on this branch — panels derive sibling hostnames CF doesn't have; acceptable pre-release, per-env sequencing protects production.)
- **Phase 2 — relay + tunnel data plane.** `apps/share_relay` project; connector `shares` tables + `POST /shares` + `/frps/auth`; template frpc + Caddy + gateway serving with a locally-generated self-signed cert in dev. Result: a dev workspace reachable through a dev relay end-to-end (cert warnings accepted, no auth yet — gateway denies all but a test allowlist).
- **Phase 3 — certs + auth.** Connector ACME/CSR endpoint with multi-CA config; accounts login UI + broker authorize + JWKS; gateway callback/cookie/grants/Origin enforcement; renewal loop. Result: full authenticated share flow works in dev with real certs.
- **Phase 4 — minds integration + CF deletion.** Sharing handler rewrite, machines API, settings UI, provisioning/status UX; delete all Cloudflare code from connector, imbue_cloud client/CLI, minds; template drops cloudflared. Result: the complete v1 feature, dev-green.
- **Phase 5 — staging soak + release.** Deploy staging relays/connector/DNS/accounts domain; seeded test accounts; release tests green; manual checklist; then production cutover via a minds release (template + mngr pinned in lockstep). CF account cleanup deferred to follow-up.

## Testing strategy

- **Unit** — mngr_forward host parsing/resolver/cookie/TLS minting; gateway grants evaluation (workspace/service/domain grants, malformed-file fail-closed), token verification (static JWKS fixture, aud/exp/jti), Origin policy matrix, Caddyfile rendering; connector share endpoints, relay-token auth, frps-auth op handling, hostname/region derivation; forward_port name validation; frontend `deriveServiceOrigin` + layout restore (vitest).
- **Integration (CI, dockerized)** — harness: frps container + workspace container (Caddy + gateway + stub services) + fake broker (local signing key/JWKS) + Playwright Chromium trusting a test CA; full flow: login handoff, shell + iframe panel + WebSocket through the relay, per-service 403s, revocation mid-session, service registered while shared, unshare teardown. Connector ACME tested against Pebble (Let's Encrypt's test server) with a mock DNS hook. mngr_forward local origin tests extend the existing server_test harness.
- **Acceptance/release (staging)** — `@pytest.mark.release`: real share via desktop API, real ACME cert, real relay; second seeded account logs in through the real accounts page in a fresh browser; panels + WebSocket verified; per-service grant; revoke; unshare; re-share (cached cert fast path); renewal endpoint exercised with a short-validity CSR.
- **Manual checklist** — Safari + Firefox on a real share; deep links to service origins; two workspaces open simultaneously (cookie isolation); relay restart mid-session (frpc reconnect); connector briefly down (existing sessions keep working); paused host behavior; provisioning UX timing.
- **Edge cases** — service label colliding with reserved prefixes; grants email case-normalization; expired/replayed handoff token; state-nonce mismatch; cert expiry while host paused (renewal on next start; re-share path); frps thundering-herd reconnect after relay restart.

## Open questions

- frp spike outcomes could reroute: exact passthrough behavior for wildcard `customDomains`, plugin-op payloads, and whether frps needs patching (fallback: sish or ~500-line custom SNI router).
- Session cookie name (`imbue_machine_session`?) and whether 24h lifetime should be configurable per share.
- Should the `web` placeholder service be excluded from workspace shares by default (it's noise), or is all-services-means-all-services cleaner?
- Broker keep-warm sizing and Modal custom-domain limits for `accounts.imbue.com` (+ per-env twins).
- Relay instance flavor final pick (per OVH region availability) and frp version pin/upgrade cadence.
- Comms for the cutover: existing CF shares die at release — in-app notice? release notes only?
- CT-log exposure accepted (opaque ids) — do we want a standing audit item that user ids are never treated as secrets anywhere?
- Whether the loading page for unregistered services is served by the gateway (authenticated) or shown to unauthenticated users too (information disclosure trade-off: reveals that a workspace exists).
- Grants document size limits / max emails per share.
- Exact per-dev DNS record automation (dev deploy recipe writes `*.<dev-suffix>.minds-dev.com` via the zone token — scoping a token per dev vs a shared dev token).
