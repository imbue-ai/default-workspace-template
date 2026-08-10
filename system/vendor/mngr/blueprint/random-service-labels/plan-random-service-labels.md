# Plan: random per-service hostname labels (scanner exclusion for shares)

## Overview

Today every registered service owns a *predictable* origin label -- `terminal`,
`browser`, `system_interface` -- both locally (`<label>.host-<hex>.localhost`)
and on a share (`<label>.<host-id>.<user>.<region>.<domain>`), and a shared
workspace's `frpc` claims the **wildcard** `*.<ws-domain>`, so the relay splices
any SNI under the domain into the tunnel. That means the moment a share cert is
issued, the bare `<ws-domain>` (logged in Certificate Transparency) is enough for
Internet scanners to reach the in-workspace gateway; they can't get past its
auth, but they ride the tunnel and generate constant probe traffic.

This change gives every service an **unguessable** label -- `<service>-<rand>`
(e.g. `terminal-x7k9q2w1`) -- and has `frpc` claim those **explicit** labels
instead of the wildcard, so the relay drops any SNI it wasn't told about
(scanners only learn the bare `<ws-domain>` from CT, which no longer routes).
The label is the one part of the hostname CT never sees (the wildcard cert logs
only `*.<ws-domain>`), so folding a secret into it hides it from CT while keeping
one wildcard cert. Local forwarding uses the **same** labels for consistency.

A dedicated **`auth-<rand>`** label -- not a real app -- is the single origin
that serves the gateway's public `/_auth/*` surface (just the login callback);
every app label is then collision-free (an app may use any path, including
`/_auth/...`). The broker delivers its post-login callback to that origin.

## Expected behavior

### Service labels (local and shared, identical)
- Each registered service has a persistent `label = <name>-<rand>` minted once at
  first registration and stored in `data/.state/apps.toml` next to its `name`/`url`.
  `<rand>` is 8 lowercase base36 chars (~41 bits). The label never rotates
  (bookmarks/layouts stay valid); a future explicit "rotate links" action can mint
  a new one.
- Local: the shell is `system-interface-<rand>.host-<hex>.localhost:8421`; each
  panel is `<name>-<rand>.host-<hex>.localhost`. The bare `host-<hex>.localhost`
  origin 302s to the shell label (local can serve the bare origin; a share cannot).
- Shared: `<name>-<rand>.<ws-domain>`; the shell is `system-interface-<rand>.<ws-domain>`.
  The bare `<ws-domain>` does not route at all (no frpc claim, no DNS purpose beyond
  the CT-visible cert name).

### Name rules (so a label fits a 63-char DNS label with room for randomness)
- Service names: lowercase alnum runs joined by single hyphens; **new** names are
  strict kebab-case and capped at 24 chars; `system_interface` stays tolerated as a
  legacy underscore name (its label `system_interface-<rand>` matches the wildcard
  cert and resolves in browsers). Reserved prefixes `host-`/`agent-` and the name
  `localhost` remain rejected. `auth` is newly reserved (the dedicated auth label).

### frpc claims (shared)
- `frpc` claims one explicit `customDomains` entry per registered service label plus
  the `auth-<rand>` label -- never the wildcard, never the bare domain. Re-rendered
  and reloaded when `apps.toml` changes (same watch the Caddyfile already uses); a
  service registered while shared becomes claimable within one render cycle.

### Connector NewProxy authorization (shared)
- The frps plugin's NewProxy check changes from "claimed domains must equal
  `{<ws-domain>, *.<ws-domain>}`" to "**every** claimed domain is a single DNS label
  directly under this share's `<ws-domain>`" (i.e. `<label>.<ws-domain>` with exactly
  one leading label). The bare domain and the wildcard are both rejected.

### Broker callback (shared) -- the dedicated auth origin
- The gateway's `/_auth/verify` 302 to the broker now carries `callback_origin`
  (`https://auth-<rand>.<ws-domain>`) in addition to `machine_domain` (the bare
  ws-domain, still the share identifier + token `aud`) and `next` (the full origin
  URL the visitor was trying to reach).
- The broker validates that `callback_origin`'s host is `<label>.<machine_domain>`
  (one label under the share domain) and that `next`'s host is under `machine_domain`,
  then redirects to `<callback_origin>/_auth/callback?token=&state=&next=`. It no
  longer flattens `next` to a path (which today silently drops deep links).
- Caddy serves `/_auth/*` **only** on the `auth-<rand>` label. `forward_auth`
  enforcement still runs on every label, but that is an internal subrequest to
  `127.0.0.1:<gateway>/_auth/verify` and exposes no public path -- so app labels
  reserve nothing. The callback sets the `Domain=<ws-domain>` cookie (works from the
  auth label, covers all labels) and 302s to `next`.

### Gateway grant checks (shared)
- Grants stay keyed by service **name**. The gateway maps an incoming label back to
  its name via `apps.toml` (`<name>-<rand>` -> `<name>`); an unknown label is "not
  ours" (403/loading as today). The `auth-<rand>` label is recognized structurally
  (served before auth) and is never a grantable service.

## Contracts (the shared interfaces every project depends on)

1. **apps.toml row**: `{ name, url, label }`. `label` minted by `forward_port.py`,
   persisted, stable. A row missing `label` (legacy) is treated as unregistered by
   readers until re-registered -- acceptable under the hard cutover.
2. **`services` event** (`app_watcher` -> mngr event stream): `ServiceRegisteredEvent`
   gains `label`. mngr_forward's resolver stores `name -> (label, url)`.
3. **Label grammar**: `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`, <=63 chars; `<rand>` is
   `[a-z0-9]{8}`. The auth label matches `^auth-[a-z0-9]{8}$`.
4. **frpc customDomains**: exactly the set of registered service labels + the auth
   label, each `<label>.<ws-domain>`.
5. **Broker /share/authorize query**: `machine_domain`, `next` (full URL),
   `callback_origin` (full origin), `state`. Callback delivered to
   `<callback_origin>/_auth/callback`.
6. **Auth label lifecycle**: minted once per workspace, persisted at
   `data/.secrets/share_auth_label` (survives unshare/re-share like the cert), value
   `auth-<rand>`. The gateway owns it; the connector only validates its shape.

## Status

- **A (template share side): DONE** -- forward_port label minting, caddy label
  routing + dedicated auth label, frpc explicit claims + hot-reload,
  registry-aware gateway hostname mapping, auth-label persistence. Tested.
- **B (connector): DONE** -- per-label NewProxy authorization; broker
  `callback_origin` delivery + `next`/`callback_origin` host validation. Tested.
- **C (local mngr_forward + app_watcher): DONE.** app_watcher emits `label` on
  the `services` event; mngr_forward's resolver keeps an origin-label -> name map
  (fed alongside the name->url map), routes `<label>.host-<hex>.localhost` via it
  (falling back to name for label-less services), and 302s **HTML-navigation**
  bare-origin requests to the shell service's label origin. The 302 is gated on
  `Accept: text/html` so the non-HTML workspace readiness probe is unaffected
  (this is why the probe in D2 needs no change). Tested.
- **D (frontends + minds): DONE.** (D1) system_interface: `AppEntry`/apps.toml
  read + `apps_updated` WS payload + `deriveServiceOrigin` + all Dockview call
  sites + the layout URL->name parse now use the label (mapping label->name via
  apps.toml). (D2) minds: the services `label` is carried through the forward
  stream into the backend resolver and surfaced to the Share pane, whose
  per-service link now uses `<label>.<domain>`; the e2e terminal selector matches
  `terminal-<rand>`. Electron regexes and the readiness probe verified unchanged
  (the regexes already accept any leading label; the probe is non-HTML so the
  bare-origin 302 doesn't affect it). Tested.

## E. Cutover -- to actually test end to end
Nothing runs the new code yet. To test on the dev env: (1) **redeploy the dev
connector** (`minds env deploy`) so B's per-label NewProxy + broker
`callback_origin` are live; (2) **create a FRESH workspace** via `just
minds-start` (which rsyncs the new mngr + dwt into it) -- existing workspaces
have the old label-less registry and the old gateway; (3) verify: panels open at
`<name>-<rand>.host-<hex>.localhost`, the bare local origin 302s to the shell
label, a share's bare `<ws-domain>` no longer routes (connection refused) while
`<label>.<ws-domain>` does, the login callback lands on `auth-<rand>`, and the
Share-tab per-app link is `https://<label>.<ws-domain>/`.

## Implementation plan (sequenced by dependency)

### A. Template -- service label registry + share side (self-contained)
- `system/scripts/forward_port.py`: mint/persist `label` on upsert (keep existing on
  re-register); tighten `validate_service_name` (24-char cap for new kebab names,
  reserve `auth`); helper `mint_label(name)`.
- `system/services/share_gateway/.../caddyfile.py`: render host matchers on the
  service **labels** (from `apps.toml`), add the `auth-<rand>` site serving only
  `/_auth/*`, move `/_auth/*` off the shared/site scope onto the auth label, keep
  `forward_auth` on every label. `Referrer-Policy: same-origin` header.
- `.../frpc_config.py`: `customDomains` = explicit per-label list + auth label.
- `.../hostnames.py`: `service_for_host` becomes registry-aware (label -> name via
  `apps.toml`); recognize the auth label.
- `.../materials.py`: `load_or_create_auth_label`.
- `.../server.py`: `/_auth/verify` emits `callback_origin`; callback unchanged except
  it may run on the auth label.
- `.../runner.py`: pass the registry + auth label into caddy/frpc renders; re-render
  frpc on apps.toml change (currently only caddy re-renders).
- Unit tests for each; changelog.

### B. Connector -- NewProxy + broker (self-contained)
- `app.py` NewProxy validation: per-label-under-domain check.
- `app.py` `broker_authorize`: accept + validate `callback_origin` and full `next`;
  deliver callback there. Unit tests; changelog.

### C. Local -- mngr_forward + app_watcher (depends on A's apps.toml shape)
- `app_watcher/watcher.py`: `ServiceRegisteredEvent` gains `label`; emit it.
- `mngr_forward`: resolver stores `name -> (label, url)`; parse `<label>` origins and
  map label -> service; bare origin 302 to the shell label; primitives/tests.

### D. minds -- frontend + probes + electron (depends on A/C)
- Origin derivation from labels (the service->label map comes from the `services`
  event / system_interface API); share-link building per service label; workspace
  probes' Host header; `surface-routing.js` / `chrome.js` label regexes; sharing
  handler carries labels where needed.

### E. Cutover
- Hard cutover on this branch pair; recreate dev workspaces (no migration shims).

## Testing strategy
- Unit: label minting/validation; caddyfile label + auth-label rendering; frpc claim
  list; hostname label->name mapping; NewProxy per-label validation; broker
  callback_origin validation + delivery; resolver label routing; frontend derivation.
- Manual (dev relay): re-create a workspace, share it, confirm the bare `<ws-domain>`
  no longer routes (connection refused), a service label routes, the login callback
  lands on `auth-<rand>`, and scanners (bare-domain hits) are dropped at the relay.

## Open questions (resolved in the design chat)
- Readable-prefix + random: **yes** (`<name>-<rand>`).
- Rotation: **mint once, stable**; explicit rotate action is future work.
- Bare origin: **local 302s to shell label; shared bare does not route.**
- Dedicated auth label: **yes** (`auth-<rand>`), the only public `/_auth/*` origin.
- `mngr forward` (general users): **label-agnostic** -- routes whatever label the
  registration declares; plain mngr services keep readable names.
