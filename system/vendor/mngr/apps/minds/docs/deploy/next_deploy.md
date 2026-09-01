# Next deployment: running checklist

A scratchpad collecting everything the next staging / production deployment
must get right. Add to this doc as work lands; fold the items into the release
runbook ([release.md](./release.md)) when the release is actually cut, then
reset this doc.

Last reset: 2026-08-31, after minds-v0.4.3 was deployed to staging and
production (connector + LiteLLM + analytics, migrations 031-033, FRPS
plugin-secret rotation + header-form relay redeploys in both tiers, the
box `0b24ee94` RAID rebuild, pool re-bake at the re-cut release tag; see
[history/minds-v0.4.3.md](./history/minds-v0.4.3.md)). The desktop channel
rollout was deliberately NOT done (all channels still at 0.4.2 build
`260825un55i8ix7`).

## Code that must land before the next release is cut

- [ ] The leased-here trust-material fix (`53af57156b`, branch
  `mngr/deploy-0-4-3`): must be in whatever build is next promoted to the
  desktop channels -- any multi-device user whose device leased a workspace
  before another device adopted it hits permanent "Loading workspace" spin
  without it (see the 0.4.3 history entry).

## Deferred desktop rollout (from the 0.4.3 deployment)

- [ ] **Promote the desktop channels** when ready: point
  `apps/minds/release-channels.toml` at the chosen build (ideally one carrying
  the fix above), and bump the connector download fallback
  (`_DEFAULT_TARGET_BY_PLATFORM` in `accounts_web.py`) in the same PR per
  release.md.
- [ ] **Issue mngr-internal#746** (CSP `frame-ancestors` chrome-origin fix,
  Option B): deferred to the next release. Until then desktop-created shares
  stamp the modal.run connector origin into frame-ancestors, so the /web
  chrome cannot frame desktop-shared workspaces (known, accepted).

## Pending infrastructure maintenance (not deploy-coupled)

- [ ] **Modal audit-log stream**: no `modal_audit` data has ever arrived in
  any tier's OpenObserve. Audit-log export requires Modal's enterprise plan,
  which we may get enabled. If it lands, confirm the stream appears and gets
  the 90-day retention override; if we stay off enterprise, drop
  `modal_audit` from `specs/minds-openobserve-telemetry.md` so it stops being
  a silent expectation.
- [ ] **frps legacy path-secret route removal** (the last bullet of the
  #616/#650 item): every relay in every tier is now on the header form --
  staging and production with rotated secrets (0.4.2/0.4.3 deployments), and
  the 3 standing dev relays redeployed 2026-08-31 (dev-josh-1's two, plus
  dev-josh-2's orphaned instance re-registered under
  `relay-c3a0d1575876b86b` and deployed; the dev connectors already accepted
  the header form, so nothing broke). Remaining before removing the route:
  rotate the shared DEV-tier `sharing/FRPS_AUTH_SECRET` (set `<old>,<new>`,
  have each standing dev env redeploy its connector, redeploy the 3 dev
  relays with `<new>`, drop `<old>`), then remove the legacy path-secret
  route + its tests + the wire-compat entry (grep `CLEANUP` in the
  connector's `shares.py`).

## Carried-over post-deploy cleanup (from the 0.3.17 deployment)

- [ ] **Old `dev1` relay.** Destroy the instance (`just list-share-relays` /
  `just destroy-share-relay`), remove the `dev1` DNS records, and re-enable
  sharing on any workspace whose share row was created with region `dev1`.
- [ ] **Cloudflare account cleanup.** Delete the orphaned tunnel-era
  resources for previously shared workspaces: tunnels, DNS CNAMEs, Access
  applications, service tokens, and Workers KV entries. No product code can
  tear these down anymore, and a not-yet-updated workspace's cloudflared
  keeps its tunnel alive until this cleanup (or its `update-self`) severs it.
- [ ] **Remove the `/account` compat fields** (`max_tunnels`,
  `max_services_per_tunnel`, `tunnels`) once the desktop fleet is on
  minds-v0.3.17 or later (see `_DEPRECATED_TUNNEL_ENTITLEMENT_FIELDS` in the
  connector's `accounts.py`).
- [ ] Consider dropping the orphaned tunnel-era DB tables in a later
  migration (harmless meanwhile).
- PSL entries for the content domains remain DEFERRED (decided 2026-08-15:
  not worth it until we have more users).

## Known mixed-fleet states (accepted, no action)

These hold while older workspaces and clients remain in the fleet; each
retires on its own as workspaces `update-self` and clients update.

- **Workspace-keyed shares on pre-0.4.3 workspace content** (new with 0.4.3):
  a 0.4.3-era client (or the /web chrome via `MINDS_WEB_TEMPLATE_REF`-cloned
  stale content) sharing a NEVER-before-shared old-content workspace gets a
  workspace-keyed domain, and the old workspace's pre-fix `origin.ts` has the
  broken-service-panels bug until that workspace runs `update-self`. Existing
  shares keep their legacy domains (a re-share never changes a domain) and
  0.4.2-and-older clients never send `workspace_id`, so nothing already
  working breaks. Mostly theoretical until a 0.4.3+ client ships.
- **Leased-before-adoption client staleness** (fixed in code, heals on
  update): a device that leased a workspace before another device adopted it
  keeps stale bake-time pins and spins on "Loading workspace" until its
  client carries the `53af57156b` fix, at which point it converges on the
  synced adopted keys automatically.
- Pool slices baked from old tags accept blind grants writes (no CAS) until
  re-baked; the contract is backward compatible.
- Old workspaces keep their label-less service registrations until
  `update-self` restarts their services; meanwhile the forwarder and desktop
  route them by service name.
- Old workspaces' system_interface service-worker iframe mechanism is kept
  working by the forward proxy's 307 redirect (CLEANUP-marked in
  `mngr_forward/server.py`); `update-self` retires it per workspace.
- v0.3.11 installs can only materialize RSA client keys, so a multi-device
  user with one un-updated device cannot open a workspace created from an
  updated device until that device updates.
- v0.3.11 clients: the account page's plan section shows "unavailable", their
  sharing surface 404s until they update, and they must be restarted after a
  host-machine reboot.
- Pre-#547 clients request a workspace start while the row is still
  `stopping`; the current connector answers 409 -- retrying after the stop
  reaches `stopped` works, and updated clients wait it out automatically.
