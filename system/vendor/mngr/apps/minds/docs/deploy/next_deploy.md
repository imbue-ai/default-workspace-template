# Next deployment: running checklist

A scratchpad collecting everything the next staging / production deployment
must get right. Add to this doc as work lands; fold the items into the release
runbook ([release.md](./release.md)) when the release is actually cut, then
reset this doc.

Last reset: 2026-08-18, after minds-v0.3.17 was deployed to staging and
production (connector + LiteLLM, migrations 018-026, relay fleets in both
tiers, fleet-wide box sweeps, pool re-bake at the release tag, desktop
release published).

## Code that must land before the next release is cut

(nothing yet)

## Checklist for the next deployment

- [ ] **Observability bring-up** (OpenObserve; needs PR mngr-internal#465
  merged): dev-tier pass with its validation gates FIRST (it confirms the
  pinned OpenObserve API shapes and the Modal OpenTelemetry integration),
  then staging + production. Full runbook:
  [observability-bringup.md](./observability-bringup.md).
  - Dev pass COMPLETE 2026-08-18 (branch `mngr/deploy-otel`): instance
    `observability-dev-2` at 40.160.4.126 (`telemetry.minds-dev.com`),
    all validation gates green including the full replacement drill and
    the Modal integration (workspace secret `observability-otel-ingest`,
    bare base endpoint URL; function logs arrive in `modal_logs` with
    app/function/environment attribution, container metrics as `modal_*`
    metric streams); collectors on the dev box + all three dev relays;
    90-day retention on all four log streams. Three pinned shapes
    corrected in code (sender role `service_account`, password
    complexity, gate-stamped `stream-name` for Modal's bare paths).
    Not yet observed: Modal audit-log events (nothing audit-shaped has
    arrived; watch for them under `modal_logs` service names).
  - Staging pass done 2026-08-18 (same branch): instance
    `observability-staging-1` at 51.81.153.89
    (`telemetry.minds-staging.com`, OVH US-WEST-OR-1 -- near the staging
    Neon project in aws-us-west-2), ingress checks green, both ingest
    paths verified, collectors on the staging box + all four staging
    relays (metrics + logs from all six hosts confirmed, including
    per-qemu process metrics), 90-day retention on all four log
    streams, workspace secret `observability-otel-ingest` created in
    `minds-staging`. Outstanding: the manual Modal workspace
    OpenTelemetry integration (endpoint
    `https://telemetry.minds-staging.com`, select the secret), then
    confirm `modal_logs` traffic -- DONE, real Modal function logs and
    `modal_*` metric streams confirmed arriving.
  - Production pass done 2026-08-18 (same branch): instance
    `observability-production-1` at 15.204.75.104
    (`telemetry.imbueminds.com`, OVH US-WEST-OR-1), ingress checks
    green, both ingest paths verified, collectors on all 21 production
    boxes (full `just prep-server` sweep, zero failures) + all four
    production relays (metrics from all 26 hosts confirmed, including
    per-qemu process metrics on every box), 90-day retention on all
    four log streams, workspace secret `observability-otel-ingest`
    created in `minds-production`. Two more fresh-boot races found and
    fixed in code (deploy now waits for sshd; collector install rides
    out the dpkg lock held by unattended-upgrades) plus a root-password
    complexity warning in the template/runbook. Modal workspace
    integration configured and confirmed: real function logs and
    `modal_*` metric streams arriving. ALL THREE TIERS COMPLETE;
    remaining follow-ups are the runbook's post-bring-up items
    (connector `/healthz` probe after Bugsink, the deployment test,
    dashboards).
- [ ] **Fleet collector rollout** (same runbook, procedure step 5): one
  `just prep-server <id>` pass per existing box and an
  `observability install-collector` per existing relay, then re-run
  `just provision-observability-accounts <instance-ip>` once data flows so
  the log-stream retention overrides land. (Done for the dev fleet
  2026-08-18; staging/production fleets pending their tier bring-up.)

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
  connector's `accounts.py`; the removal is connector-side only -- the client
  wire models in `libs/mngr_imbue_cloud`'s `wire_types.py` no longer declare
  these fields, since tolerant parsing ignores unknown fields).
- [ ] Consider dropping the orphaned tunnel-era DB tables in a later
  migration (harmless meanwhile).
- PSL entries for the content domains remain DEFERRED (decided 2026-08-15:
  not worth it until we have more users) -- each region runs on its wildcard
  DNS record set alone, so cross-user cookie isolation between shared
  workspaces is weaker until the PSL entries are eventually submitted (PSL
  propagation is slow; revisit when user volume justifies it).

## Known mixed-fleet states (accepted, no action)

These hold while pre-0.3.17 workspaces and clients remain in the fleet; each
retires on its own as workspaces `update-self` and clients update.

- Pool slices baked from old tags accept blind grants writes (no CAS) until
  re-baked; the contract is backward compatible.
- Old workspaces keep their label-less service registrations until
  `update-self` restarts their services, at which point `forward_port.py`
  mints origin labels for legacy rows automatically; meanwhile the forwarder
  and desktop route them by service name.
- Old workspaces' system_interface still renders service panels as iframes on
  its own origin behind a service-worker bootstrap whose `document.cookie`
  write the partitioned content embedding rejects; the forward proxy
  307-redirects those navigations to the service's own origin
  (CLEANUP-marked in `mngr_forward/server.py`) so pre-update workspaces keep
  working terminals. `update-self` retires the mechanism per workspace.
- v0.3.11 installs can only materialize RSA client keys, so a multi-device
  user with one un-updated device cannot open a workspace created from an
  updated device until that device updates (client-side limitation; fixed by
  updating).
- v0.3.11 clients: the account page's plan section shows "unavailable"
  against the current connector, their sharing surface 404s until they
  update, and they must be restarted after a host-machine reboot.
