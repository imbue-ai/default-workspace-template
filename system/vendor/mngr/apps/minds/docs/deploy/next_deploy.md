# Next deployment: running checklist

What the next deployment must get right. Add items as work lands; **discharge
them as the release that ships them concludes** (step 12 of
[ops/app-release.md](./ops/app-release.md)), and reset this doc.

This file is a *queue*, not an archive. An item that has shipped belongs in that
release's [history](./history/) entry, not here. If an item cannot be stated as
something the next deployment will do or check, it does not belong on this list.

Last reset: 2026-09-03, after minds-v0.5.0 was baked to the production pool and
promoted to the alpha channel; see
[history/minds-v0.5.0.md](./history/minds-v0.5.0.md). The production services
deploy was deliberately not done.

## Must happen in this release

- [ ] **Deploy the production services.** Production runs connector
  `dabb19b95b`, whose `FALLBACK_BRANCH` is `minds-v0.4.3`, so browser creates
  (`/hosts/claim`) pin to that tag while desktop 0.5.0 clients ask for
  `minds-v0.5.0`. Until this deploys, keep `available` rows at `minds-v0.4.3`
  -- `/hosts/claim` matches the tag exactly and has no rebuild fallback.

- [ ] **Promote 0.5.0 past alpha.** Beta and stable are still 0.4.2 (build
  `260825un55i8ix7`), so most users are two releases behind. The 0.5.0 build is
  `260902shwco3ynx`.

  Bump the connector download fallback (`_DEFAULT_TARGET_BY_PLATFORM` in
  `accounts_web.py`) in the same PR **only when the channel is `stable`** -- it
  is what the public download link serves while the feed is unreadable, and
  leaving it *ahead* of stable is unrecoverable, since `allowDowngrade` is false.

- [ ] **Bake the production pool at whatever tag is promoted**, before the
  services deploy that pins to it.

## Should land soon

- [ ] **`env deploy` ships the working tree, with no ref guard.**
  `per_env_deploy.py` resolves the app file from the repo root and
  `modal deploy`s whatever is on disk -- a dirty tree, a stale branch, or a
  detached tag checkout all deploy silently. Consider refusing a dirty tree or
  one behind `origin/main`, with an override for the deliberate cases.

- [ ] **`/version` exposes no git SHA.** It returns `deploy_id` and
  `generation_id` only, so recovering the deployed commit means grepping the
  deploy id out of a hand-written history entry -- which exists only if somebody
  wrote one. Stamp the deployed commit into the deploy-metadata secret.

- [ ] **Modal audit-log stream**: no `modal_audit` data has ever arrived in any
  tier's OpenObserve. Export requires Modal's enterprise plan. If that lands,
  confirm the stream appears and gets the 90-day retention override; if we stay
  off enterprise, drop `modal_audit` from
  `specs/minds-openobserve-telemetry.md` so it stops being a silent expectation.

- [ ] **Remove the legacy frps path-secret route.** Every relay in every tier is
  on the header form. Remaining: rotate the shared DEV-tier
  `sharing/FRPS_AUTH_SECRET` (set `<old>,<new>`, have each standing dev env
  redeploy its connector, redeploy the 3 dev relays with `<new>`, drop `<old>`),
  then remove the route, its tests, and the wire-compat entry -- grep `CLEANUP`
  in the connector's `shares.py`.

- [ ] **Remove the `/account` compat fields** (`max_tunnels`,
  `max_services_per_tunnel`, `tunnels`) once the desktop fleet is on
  minds-v0.3.17 or later -- see `_DEPRECATED_TUNNEL_ENTITLEMENT_FIELDS` in the
  connector's `accounts.py`. Blocked until a **stable** promotion carries the
  fleet past 0.3.17; alpha alone does not.

## Accepted, no action

These hold while older workspaces and clients remain in the fleet, and each
retires on its own as workspaces run `update-self` and clients update. Listed so
nobody re-investigates them.

- Pool slices baked from old tags accept blind grants writes (no CAS) until
  re-baked; the contract is backward compatible.
- Old workspaces keep label-less service registrations until `update-self`
  restarts their services; the forwarder and desktop route them by name meanwhile.
- Old workspaces' service-worker iframe mechanism is kept working by the forward
  proxy's 307 redirect (CLEANUP-marked in `mngr_forward/server.py`).
- v0.3.11 installs can only materialize RSA client keys, so a multi-device user
  with one un-updated device cannot open a workspace created from an updated one.
- Pre-#547 clients request a workspace start while the row is still `stopping`;
  the connector answers 409 and updated clients wait it out.
- PSL entries for the content domains: deferred by decision (2026-08-15), not
  worth it until we have more users.
