# minds-v0.3.17 (2026-08-17/18): shipped to staging AND production

The first production deployment since minds-v0.3.11. Final tag pair: mngr
`4cafba1563`, default-workspace-template `b8976f141` (vendor-match verified
by blob-hash comparison); launch-to-msg green on the tags; ToDesktop build
`260817h3zit4rtw` published.

## Release blocker found and fixed (tags re-cut once)

The first tag pair failed launch-to-msg: the 0.3.17 template newly declares
`[agent_types.codex]` and `[agent_types.pi-coding]` in `.mngr/settings.toml`,
and the packaged desktop app's mngr did not bundle `imbue-mngr-codex` /
`imbue-mngr-pi-coding` -- so EVERY workspace create from the shipped binary
failed with "Unknown fields in agent_types.codex". Never seen in dev because
source runs use the full monorepo venv. Fix: both plugins added to all four
mirrored workspace-package lists (`apps/minds/scripts/build.js`,
`electron/env-setup.js`, `electron/pyproject/pyproject.toml`,
`scripts/build_test.py`). A snapshot-resume guard test now asserts every
plugin-provided agent type in the template is bundled.

## Deployments

- Staging: deploy_id `20260817T181401Z` (migration 026; generation id
  unchanged).
- Production: deploy_id `20260817T200846Z`; migrations 018-026 all applied
  in one deploy (020 drops tunnel entitlements, 024 stop/start, 025 relays,
  026 attribution); custom domains `accounts.imbue.com` + `minds.imbue.com`
  attached; RECREATE strategy; zero placeholder values.

## Production relay fleet (brought up immediately after the deploy)

| region | instance | instance_id | ip | relay_id |
|---|---|---|---|---|
| us1 | share-relay-production-us1-1 | 74d676aa-46ac-44e1-85cb-9778e2def822 | 15.204.73.79 | relay-00a586332d39770b |
| us1 | share-relay-production-us1-2 | 25643799-4cac-4980-8dda-91d1b68c6471 | 15.204.75.189 | relay-a2ca24723fa3900d |
| us2 | share-relay-production-us2-1 | 691f3ddb-ad4a-4f90-8514-62b10be87987 | 40.160.4.254 | relay-0a2a7c6a8be7a531 |
| us2 | share-relay-production-us2-2 | 088423ca-3c44-4cda-a73c-d78ef4c5f352 | 147.135.77.174 | relay-29b244661515f18b |

Content domain `imbueminds.com` (the production `cloudflare` Vault entry is
that zone). Bring-up: provision (OVH, `relay-ssh` key) -> register (admin
key; needs migration 025) -> deploy frps over SSH -> `dns-share-relay` per
region -> all four `healthy` in `admin relays list`. Gotcha: `vault kv get
-field=value` strips the SSH key's trailing newline and ssh-add then rejects
it -- re-append `\n`.

## Fleet ops sweep (all 21 production boxes, piloted on one first)

`prep-server` x21, `backfill-autostart` 188/188 VMs, `repair-keys` 188/188
VMs; final `audit-boxes`: 21/21 tier-exclusive, zero degraded RAID arrays,
zero raw swap devices. Pool re-baked at the tag: 29 available slices
US-WEST-OR + 26 US-EAST-VA.

## Incidents and lessons

- **lima hostagent tunnel wedge**: a VM whose forward ports accept TCP but
  serve no SSH banner (reset mid-banner) while the VM itself runs fine has a
  wedged hostagent<->guest-agent tunnel channel (`ha.stderr.log` shows
  `could not open tunnel ... grpc: the client connection is closing`; the
  guest-agent EVENT stream can still be alive). Outbound traffic (e.g.
  cloudflared) is unaffected, so the workspace looks healthy. Heal: restart
  the VM (`limactl stop`/`start`) -- but only after the box has had the
  sweeps, so keys survive the cidata replay and services auto-start. Found
  on two boxes (`feb11eae`, wedged since Jul 30 / Aug 5; `c011f511`).
- **Stale leases from the release_host 303 bug** (mngr-internal#446): three
  leased rows on `c011f511` had empty VMs and wiped data disks -- the users
  had destroyed their workspaces (Aug 3-4) and only the lease release never
  landed. Destroyed with `--force` after verifying no data existed.
- **Big-box concurrency**: a 14-slice parallel bake on the 29-slot box
  `a7828ee9` (15 pre-existing slices) failed 6/14 with `failed to listen
  tcp` port-bind collisions against existing slices' ports, plus image-load
  errors; all failures rolled back cleanly. Investigate its port
  reservation before big bakes there (still-open follow-up).
- **OVH hardware trail**: production box `0b24ee94` (51.81.185.229) lost an
  NVMe on Aug 7 (controller off the bus); tickets #720523/#720852; component
  replaced Aug 14; verified `[2/2] [UU]` on both arrays. Staging box
  `21ae4720` (15.204.52.75) is still single-disk; ticket #721263 open --
  drive S/N and a "next few days" window were provided 2026-08-18, awaiting
  OVH scheduling. After the intervention, verify both NVMes + resync + VM
  autostart.
- Modal control plane is not read-your-writes consistent after `modal
  deploy`: a function lookup can transiently 404 right after its own deploy
  (worse under concurrent same-app deploys). `deploy_function` now retries
  that lookup briefly; seen as flaky modal create tests in acceptance CI.

## Client-compat notes shipped with this release

Existing sessions re-login once (partitioned cookie). v0.3.11 clients: plan
section reads "unavailable", sharing 404s, and a restart is needed after a
host machine reboot -- all until they update. The remaining post-deploy
cleanup items live in [next_deploy.md](../next_deploy.md).
