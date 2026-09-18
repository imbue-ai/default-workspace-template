# Notes toward the gen2-turnover.md runbook (phase-5 deliverable)

Raw observations from the dev canary's conversion/drain/management-plane session
(2026-08-24, second canary session). To be folded into
`apps/minds/docs/deploy/gen2-turnover.md` when phase 5 writes it.

## Drain ergonomics

- `minds-admin server drain --server-id <id>` on a gen-1 box with one leased +
  one available row: one command did everything (status -> draining, available
  row destroyed with its VM, leased workspace force-stopped through the admin
  stop). Re-run while the force-stop was still `stopping` was a clean no-op
  (`stopping`/`stopped` rows are not destroy-eligible, so a re-run can never
  eat a stopping workspace).
- The force-stop of a thin, freshly-baked gen-1 workspace took ~9 minutes
  end-to-end (lima artifact upload from a vin box).
- Drain leaves the box `draining`; remember `minds-admin server set-status`
  back to `ready` on a box that is NOT actually being repaved (dev-only move;
  production drains proceed to repave).

## Restore/conversion routing

- Restore candidates are region-filtered by the ROW's lease-region label
  mapped to the datacenter code. A drained box's workspaces restore only
  within their label's region -- when turning over a region, make sure gen-2
  capacity exists in that same region BEFORE draining gen-1 boxes, or starts
  fail with "no capacity available right now".
- A draining origin box never restarts in place, even within the retention
  window (the restore path takes over and reaps the abandoned local VM after
  landing elsewhere).

## Conversion timing

- A thin gen-1 workspace's conversion restore (download + first boot + the
  strict bookworm->trixie dist-upgrade + reboot + banner verification) took
  **~5.8 minutes** end-to-end on the dev canary box -- far inside the 1-hour
  supervisor poll bound. The upgrade portion itself was ~3-4 minutes.
- The STRICT failure mode verified live: a conversion whose container never
  came back landed the row on `stopped` with `attributes.guest_upgrade_failed`
  set and the reserved candidate slot fully rolled back (no leaked VM or
  instance dir); the next start re-ran the whole conversion cleanly.
- Conversion bugs the canary caught (all fixed on this branch): partitioned
  gen-1 data disks (mount the filesystem-bearing node, not the raw disk),
  lima's per-boot cloud-init hook surviving conversion, the grow oneshot
  matching the iso9660 cidata mount, and disk grows not landing in-guest
  without a growpart of the partition first.
- Resize of a converted machine verified end-to-end: 8->16 units + 29->40G
  disk applied at an in-place restart in ~2.5 minutes (RAM, vCPUs, qcow2,
  partition+filesystem grow, container memory cap, connector restamp).
- The measured-size restamp at stop (the "lazy disk backfill" mechanics)
  works: the converted row's disk_gb went 28 (assumed default) -> 29
  (measured qcow2 virtual size). The `machine_disk_backfilled` metric stays 0
  legitimately -- it fires only for rows whose disk_gb is NULL, and no such
  row exists in dev anymore.

## Management plane

- Modal Proxy lookup is environment-scoped and the workspace's proxy-IP limit
  is 1 on the current plan: the dev tier's shared proxy must live in ONE
  Modal environment (`main`) and be resolved by name + environment (the
  `[modal_proxy].environment_name` field added in this session). Creating the
  proxy is possible via the Modal API (ProxyCreate/ProxyAddIp RPCs) -- no
  dashboard needed.
- Proxy egress verified: a connector-shaped function attached to the proxy
  egresses from the proxy's static IP (checked via api.ipify.org).
- The proxy attach initially failed container startup ("Function has 10
  dependencies but container got 11 object ids"): the proxy name env var was
  deploy-subprocess-only, so read_modal_proxy returned None when the
  container re-imported app.py. Fixed by forwarding the proxy vars into the
  containers through an inline secret (`forwarded_env_secret`).
- The activation sequence held up live: deploy-with-proxy first (lease/stop
  cycles worked through the proxy before any lockdown), then re-prep the box
  (WireGuard + :22 lockdown in one converged prep). Post-lockdown: new laptop `:22`
  connections drop, the per-slice port range stays open, operator SSH works
  over the WireGuard overlay (10.202.1.1, pinned host key, ~60ms vin<->hil), and a
  full connector-driven stop/start cycle succeeds via the proxy's
  allowlisted egress.
- Post-lockdown operational gap to plan around: `minds-admin pool create`
  (bakes) and `server prep` SSH the box's PUBLIC :22 from the operator's
  machine, which the lockdown now drops. Bakes/re-preps of a locked-down box
  need either an allowlisted egress (the Modal-sandbox bounce host) or
  overlay-address support in the tooling. On dev this was sidestepped by baking
  before the lockdown landed.

## Operator transport (onetun) and overlay renumbering (2026-08-25, canary restock session)

- The post-lockdown operational gap above is CLOSED: the dial resolver's
  userspace onetun transport carried the whole restock end to end. The
  occupancy audit, both renumbering preps, and the bake all reached the
  locked-down box with no kernel tunnel up and the public `:22` verified
  refused from the operator machine.
- onetun is pinned (0.3.10) and installed by `minds-admin wireguard
  install-onetun` (sha256-verified per platform, well-known path
  `~/.minds-wireguard/bin/onetun`). The real binary's CLI matches the argv
  contract the tests pin (`spec --endpoint-addr --endpoint-public-key
  --source-peer-ip --keep-alive`, key via `ONETUN_PRIVATE_KEY`) -- verified
  against `onetun --help` and live dials before first use.
- The overlay renumbering (10.202.0.0/16 plan -> the per-tier
  `10.112.0.0/16` dev carve) converged in exactly the documented two-prep
  shape: run 1 restamped the row `10.202.1.1 -> 10.112.1.1` (dual-written to
  the legacy column) and died with `Timeout, server 127.0.0.1 not
  responding` when the box's `wg0` restarted mid-run; run 2 completed the
  full idempotent prep over the userspace tunnel with the NEW identities
  (source `10.112.0.2` -> box `10.112.1.1`).
- Run 1's dial needs the OLD identities (the box's live `wg0` still has the
  old peer plan). The runbook assumes a `wg-quick` kernel tunnel for that,
  which needs root; a no-root equivalent worked: point `MNGR_ONETUN_PATH` at
  a wrapper that rewrites `--source-peer-ip` to the operator's OLD overlay
  address. Same tunnel semantics, no sudo. Worth remembering for
  staging/production renumberings run from restricted machines.
- Ordering fact the renumbering relies on: `server prep` resolves its dial
  from the row's CURRENT (pre-restamp) address, then restamps, then SSHes --
  so run 1 dials the old address and run 2 the new one. Migration 034 must
  be applied before ANY command that reads `bare_metal_servers`.
- The restock bake (`just bake-slice-dev US-WEST-OR "" 1 --server-id ... --repo-branch-or-tag
  mngr/new-fleet-testing`) took ~13 minutes: carve SSH over the userspace
  tunnel, container build over the PUBLIC slice ports (the management/user
  split held). The row landed `available` with `vps_address` = the public
  address, sizes stamped (8 units / 28G, gen 2), and the VM answers root SSH
  on the public port 22000 (trixie guest) -- the dev pool is restocked.
