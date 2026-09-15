# minds-v0.6.0 (2026-09-13): the gen-2 slice-fleet release, cut and built; dev and CI on gen-2

Tag pair: mngr `5325e15e73` (reachable on `main` as the parent of merge
`ce211576e7`, PR #966), default-workspace-template `96935db5b` (DWT PR #584's
merge into `main`), both `minds-v0.6.0`, annotated; vendor-match verified
(`system/vendor/mngr` equals `git archive` of the mngr SHA modulo `.minds/`). ToDesktop build `260913czqewar13`
(launch-to-msg run 34742645871 on the tags: every job green, `build`,
`macos_launch`, `launch_to_msg` and `notify_slack`; a real 36 minute run,
not a marker-cache skip).

The first release line that runs on gen-2 boxes: the bake-time guard pairs
`minds-v0.6+` tags with gen-2 boxes and older tags with gen-1, and 0.6.x clients
send `max_box_generation = 2` on every lease. This entry records the cut, the
dev + CI rollout, and the staging beachhead (the services deploys and the
first gen-2 repave, in the "Staging tier" section below). **Nothing was
deployed to production**, which has no gen-2 box, and no channel was promoted;
the incremental migration (alpha user first, then alpha, beta, stable cohorts)
is the next step, per [../gen2-cutover.md](../gen2-cutover.md).

## What landed before the cut (PR #966 on top of the gen-2 merge, PR #870)

- The CI warm-cache job and the release teardown's stale-slice sweep run
  under the `ci-infra` activation through the deployment-test orchestrator, so
  they dial a gen-2 CI box with a certificate the ci tier's Vault SSH CA signs
  (the runner's `minds_ci_env_gh` token signs it; the box's sshd logs the
  login as `ED25519-CERT ... ID operator:admin-auth-jwt_...`).
- `test_latchkey_e2e` fixed twice: the in-process backend registry lacked the
  docker backend (the autouse fixture's local-only load is sticky), and the
  provider `ConcurrencyGroup` was never entered.
- `test_workspace_stop_start` un-gated (its stop deadline 30 min instead of
  3.5 h) and the snapshot job installs Playwright Chromium so the three
  browser-driven services tests run instead of skipping.
- Version 0.6.0, `FALLBACK_BRANCH = minds-v0.6.0`, and the 0.4.0 wire-compat
  snapshot's window extended to 2026-10-13 (only additive optional fields and
  one tolerantly parsed route since 0.5.2).

## Dev tier (dev-josh-2)

- Connector + LiteLLM proxy redeployed from the merge commit `ca96497626`
  (deploy_id `20260913T004743Z`, `MINDS_WEB_TEMPLATE_REF=minds-v0.6.0`,
  RECREATE, no pending migrations; schema at 041).
- The three gen-2 boxes `716159c2` (vin), `d7c0d9a3` (vin), `45dcd2a4` (hil)
  re-prepped from the merged tree: each now carries the S3 IPv4 pin
  (`mngr-s3-ipv4-pin.timer` active, two IPv4 lines in `/etc/hosts`) and its
  entries in the telemetry integrity manifest (23 entries, all OK).
  `server list --verify-occupancy`: 4 exclusive, 0 contaminated; keys 0/0
  and CA correct on the gen-2 boxes; storage encrypted. The gen-1 box
  `f90b4e1d` was left alone on purpose (it holds foreign lima VMs and is the
  migration-test fallback).
- The four `available` rows baked from the deleted provisional tag were
  destroyed (4/4). **Trap hit at the re-bake:** all three boxes still held a
  `default-workspace-template-minds-v0.6.0.tar` in `/srv/mngr-slices/image-cache`
  from the provisional bakes (the cache keys on the tag *name*), so the first
  `pool create --from-tag minds-v0.6.0` loaded the old content without a
  warning. Those rows were destroyed, the stale tar deleted on every dev box
  (`sudo rm /srv/mngr-slices/image-cache/default-workspace-template-minds-v0.6.0.tar`),
  and the bake re-run: fresh seeds on vin (`716159c2`) and hil (`45dcd2a4`),
  2 rows each, content verified inside a slice container over the operator
  certificate (`FALLBACK_BRANCH = "minds-v0.6.0"`, `version 0.6.0`,
  `system-services` STOPPED, neutral git identity, deferred install complete,
  gVisor kernel). This is exactly why the runbook says a tag name must never be
  reused once anything has run against it; Josh chose to reuse `minds-v0.6.0`
  knowing the provisional tags had been deleted, and the cache was the one
  place the old name survived.
- Fast-path lease from the CLI (an isolated `MNGR_HOST_DIR`, the alpha test
  account): `FAST PATH: adopted pre-baked agent` on the vin box, the workspace
  answered `mngr exec` with 13 RUNNING / 2 EXITED supervisor programs and
  `FALLBACK_BRANCH = "minds-v0.6.0"`. Stop/start timing with 4 GiB of extra
  data: stop requested 06:48:49Z, row `stopped` 06:51:29Z (2 min 40 s for
  the VM halt, snapshot and the ~15 GB upload; every s5cmd connection on the
  box went to 51.81.92.24, the pinned IPv4 address); retention finalize
  cleared the slot at 06:54:03Z (dev's 300 s window); start requested at
  once, `leased` at 06:56:10Z (2 min 07 s) on a *different* box (`d7c0d9a3`),
  with the blob's checksum intact and supervisord up. Main's gen-1 numbers
  were 10-40 min for the upload alone.

## CI tier (cutover phase 3, "CI split fleet")

- `ee903147` (vin) repaved gen-2 with `minds-admin cutover repave
  --yes-i-mean-dev` from the `ci-infra` activation (report
  `~/.minds-ci-infra/cutover/reports/repave-20260913T010701Z.json`):
  `ready`, 936 GB storage partition, encrypted, CA correct, open `:22` (the
  ci tier has no `[management_plane]` on purpose). `7e76f6be` (hil) repaved
  the same way once the first gen-2 dispatch was green (both boxes now gen-2,
  encrypted, CA correct; audit 2 exclusive / 0 contaminated).
- Release dispatch 34729743185 (first gen-2 run): warm-cache, ci-env build
  (import-boxes + a 6-slice bake on the gen-2 box), offload, acceptance,
  docker, snapshot-resume all green; `minds_services` 21 passed / 6 skipped
  (3 relay tests skip by design on per-run envs; 3 Playwright tests skipped
  for the missing Chromium, since fixed); the three `minds_deployment` tests
  passed; `test_latchkey_e2e` failed on the concurrency-group bug (since
  fixed). Teardown and the gen-2 sweep succeeded.
- Gen-2 pool timings from that run: stop/start full cycle 201 s (was ~2.6 h
  on gen-1), machine resize 206 s, fast-path create 124 s, slow-path rebuild
  381 s, lease isolation 54 s.
- Release dispatches 34732245157, 34734683935, 34737169924 and 34739592741
  (each carrying one more fix) shook out the tests that had never run in CI
  before: `test_latchkey_e2e` (registry, concurrency group, `docker_sdk`
  mark), `test_web_chrome` (the overview hides destroyed workspaces behind a
  toggle since 2026-08-13), and two live-dependency flakes now marked
  (`test_storage_cleanup_grant_cycle`: a Cloudflare round trip over 60 s;
  `test_workspace_docker_container_is_present_and_stopped`: sandbox ordering).
  On the final head 5325e15e73 the release job is green (3 `minds_deployment`
  + 5 plain release tests, AWS opt-in skipped) and `minds_services` is 23
  passed / 3 relay skips / 1 already-flaky mail.tm delivery timeout; every
  services test passed on gen-2 in at least one of the last three runs.

## Staging tier (2026-09-13, afternoon)

- Prerequisites landed in commit `3e97dd1ea2` (this PR): the staging `[ssh_ca]`
  (the `minds-staging-ssh` CA public key), the staging and production
  `[management_plane]` tables (Josh's operator peer at `10.96.0.2` /
  `10.64.0.2`, whose private keys live at `~/.mindsadmin/<tier>/wireguard.key`
  on his dev box, and each tier's Modal Proxy `mind-connector-east`, created by
  hand in the tier's Modal workspace in Modal's `us-east` region: staging
  `98.90.51.49`, production `52.206.40.121`). The connector AppRole's role-id and
  a fresh secret-id were minted into `secrets/minds/staging/ssh-ca`.
- Connector + LiteLLM proxy + analytics deployed from `3e97dd1ea2` (deploy_id
  `20260913T160033Z`, RECREATE): migrations 034-041 applied (staging had been at
  033 since the 0.4.3 deploy `20260830T160320Z`; 039 stamped `disk_gb` on all 30
  rows, 040 `uplink_mbps` on all 3 boxes), `ssh_cert_refresh` seeded the
  certificate Dict, both health checks green, the Neon snapshot deleted. The web
  create pin moved to `minds-v0.6.0` (no such rows exist yet, so browser creates
  on staging 503 until the first gen-2 bake). Verified afterwards: `/version`
  advanced, `minds-admin server list` reads the tier again (it could not before
  041), all 4 relays healthy, and the live `rsc-staging` container's egress IP is
  the proxy's `98.90.51.49` -- while `MODAL_REGION` was `spaincentral`, like the
  dev container observed in `eu-central-2` earlier the same day: nothing pins
  our Modal functions to a region, so containers land anywhere and tunnel back
  to the us-east proxy (see `next_deploy.md`).
- Before the beachhead: 3 gen-1 boxes, 19 `available` rows at 0.4.1/0.4.2/0.5.1,
  10 leased + 1 stopped workspaces across 6 users (hynek, qi, weishi, mark,
  gabriel, russ; Josh's account held no remote workspace). Every box held leased
  rows, so the beachhead came from a drain rather than an empty box.
- **Old-client rows**: 2 `minds-v0.5.2` rows baked on `c7793839` (hil,
  `US-WEST-OR`) from a fresh seed (no prior 0.5.2 tar on the box), after
  destroying 5 never-leased 0.4.1 `available` rows there for slots. Verified in
  the containers: deferred install complete, `system-services` STOPPED, neutral
  git identity, `git describe` = `minds-v0.5.2`, vendored `FALLBACK_BRANCH` =
  `minds-v0.5.2`. The runbook's `CONTENT_OK` grep read stale because at 0.5.2
  the `provider-chooser` marker lives in the chat app's source, not under the
  `system_interface` static path it greps. The held-back `minds-v0.5.2` desktop
  (the version staging was last started with on 2026-09-10, from
  `/home/user/.mngr/worktrees-test/client-0-5-2`) fast-path adopted both
  (`workspace-1` -> row `9c563e46`, `workspace-2` -> row `22c47e3f`, each create
  under a minute); these are the migration-test workspaces.
- **Beachhead `21ae4720`** (hil, 24rise01, 64 GB): `server drain` destroyed its
  2 available rows and force-stopped its 4 leased workspaces (hynek x2 at 0.3.10,
  qi at 0.3.11, weishi at 0.5.1; stops requested 16:34Z, uploads done by 16:47Z).
  The stopped rows then held the box link for the full hour-long retention
  window (`repave` refuses a box any `stopped` row still points at), so the
  repave could only start at 17:35Z; `cutover repave --yes-i-mean-staging` took
  14 minutes and left the box gen-2 `ready`: overlay `10.96.1.1`, LUKS storage
  on `/dev/md4` (header backup uploaded), 391 GiB disk budget / 56 units, CA
  trust, collector, and the tier's first `:22` lockdown (allowlist `wg0` +
  `98.90.51.49`; public `:22` refused, the onetun operator dial works). Audit:
  3 exclusive / 0 contaminated, the new box keys 0/0, CA correct, encrypted.
- **Retention window**: that hour was pure wait (the parked VM is a
  restart-latency optimization; the durable artifact is verified before a row
  reaches `stopped`, and restart-in-place already excludes a draining box), so
  staging and production now set `[storage] stop_retention_seconds = 600`
  (commit `a16ad57537`). Staging redeployed to pick it up: deploy_id
  `20260913T171558Z`, ROLLOVER, no migrations, from `a16ad57537`.

- **0.6.0 rows on the beachhead**: 2 x `minds-v0.6.0` baked on `21ae4720`
  (container ports 22001 and 22003; fresh seed, no prior 0.6.0 tar on the
  box), verified inside the containers: gVisor, `FALLBACK_BRANCH =
  "minds-v0.6.0"`, version 0.6.0, `system-services` STOPPED. Still
  `available`; browser creates pinned to 0.6.0 should work again on staging
  (unverified).
- **Preflight before the drill**: both 0.5.2 workspaces CANDIDATE (version
  ok, keys rotated, data disk 29 GiB -> 45 GiB row `disk_gb`). The tier read
  NOT CLEAN for one reason only: hynek's `08bb40d7` (0.4.1 on the vin box
  `72dd8187`) fails `git describe` inside its container, so it is REFUSED.
  Irrelevant for `--workspace` selection; it must be settled (`update-self`
  by its owner, or accept losing it) before a `--source-server-id` sweep of
  `72dd8187`.
- **The old-client migration drill** (Josh's two 0.5.2 workspaces, GitHub
  granted through latchkey on both, sharing enabled on both over relay `us1`):
  - Migration 1, app RUNNING: `workspace-1` (row `9c563e46`, host
    `host-fd247c455c214630b33c705019e593e4`) -> `15.204.52.75:22004/22005`,
    21 min including the lazy 0.5.2 image-tar seed on the target. Migration
    2, app QUIT with the latchkey supervisor left running on purpose:
    `workspace-2` (row `22c47e3f`, host `host-f22171c6e18e438d9782864efa0b4b38`)
    -> `15.204.52.75:22006/22007`, 12 min (tar reused). Both rows `leased`
    on `21ae4720`, gen 2, runsc, gVisor kernel, `/home/user` intact, 13
    supervisor programs RUNNING, `git describe` still `minds-v0.5.2`.
    Reports and state under `~/.minds-staging/cutover/`.
  - PASSED: the 0.5.2 client reached both workspaces at the new address with
    no host-key prompt (its discovery re-resolved the endpoints and moved the
    adopted pins; `mngr exec` from the 0.5.2 client works), chat, terminal and
    the share URL confirmed by Josh in the UI, and each share's relay tunnel
    re-logged in from the new address within seconds of the migrate
    (`shares.last_tunnel_login_at`).
  - FAILED 1 (desktop side, imbue-ai/mngr-internal#970, fixed separately):
    the 0.5.2 latchkey supervisor kept dialing the OLD VM endpoint
    (`51.81.185.232:22002`) for 20+ cycles -- its gateway route cache is
    never invalidated, the provider's leased-hosts cache never expires, and
    the provisioned-hosts set is per session. Remedy for old clients: restart
    the app (the backend stops the old supervisor and spawns a new one; at
    19:28Z the gateways came back on both VMs with ports 1988/1989 bound).
  - FAILED 2 (migrate side): after the restart the agent reported "your
    GitHub sign-in didn't survive the restart. Re-requesting it now." The
    migrate transplanted only the data disk and harvested only sshd keys,
    `authorized_keys`, `docker inspect` and `git describe`; `/root/.latchkey/`
    (the credential store included), the supervisor confs and the tmpfs
    secrets live on the VM boot disk / tmpfs and were left behind, and the
    desktop's re-provisioning restores software, `permissions.json` and the
    tmpfs pair but never the credential store (the machine owns it). Both
    migrated workspaces lost every sign-in; Josh re-granted GitHub by hand.
    Spec: `blueprint/slice-fleet-cutover/migrate-latchkey-state.md`;
    implemented on this branch (the migrate now harvests and replays the
    whole machine-owned latchkey state and restarts the gateway). Because
    both workspaces were re-granted by hand they cannot serve as the re-test;
    a fresh 0.5.2 workspace holding a granted credential is needed.
  - Confirmed on the way: ordinary gen-2 stop/start keeps the store (the stop
    artifact carries the boot disk); only the migrate lost it. Follow-up
    idea, not scheduled: move `/root/.latchkey` onto the data disk in
    `mngr_latchkey` provisioning.
  - Migration 3 (the latchkey re-test), app OPEN on the workspace: FAILED
    before the latchkey leg could be judged. The migrate's admin stop halted
    `workspace-3`'s VM; the desktop's open stream broke, its health tracker
    went STUCK after 8 seconds and dispatched an unattended `mngr start`,
    which waited out `stopping` and landed the instant the row read
    `stopped`, before the migrate had copied the stop artifact and parked the
    row. The restart in place deleted the artifact, the migrate's S3 copy
    failed with NoSuchKey, and the workspace came back on gen-1 unharmed as
    if nothing had happened. Root cause and fix: `specs/workspace-stop-kinds.md`
    (the migrate's stop now carries a `maintenance` kind the owner's start and
    the desktop's unattended recovery both honor from the stop request
    onwards), implemented on this branch.

## Staging tier (2026-09-14): the stop-kinds connector deploy

- Connector + LiteLLM proxy + analytics redeployed from `7fa09e4905` (deploy_id
  `20260914T135700Z`, RECREATE because a migration ran, 13:57Z-13:59Z):
  migration 042 (`042_workspace_stop_kind.sql`) applied, the only pending one;
  the Neon snapshot branch `pre-deploy-20260914T135700Z` was taken and deleted on
  success, both health checks green. Verified afterwards: `/version` advanced,
  `schema_migrations` at 042, `pool_hosts.stop_kind` readable (NULL on all 28
  rows: 14 available, 9 leased, 5 stopped), `minds-admin pool list` reads the
  tier, and `minds-admin workspaces set-stop-kind` on the running `workspace-3`
  row answers the typed 409 (`WorkspaceHasNoStopError`), which is the probe
  `cutover migrate` runs before every stop. The deploy touched no running
  workspace; the branch desktop client (backend at `8833fee3d2`, no stop-kind
  desktop code) kept running through it.
- **Migration 3, second attempt (drill 1 of the stop-kinds spec's old-client
  case), PASSED**, 14:05:35Z-14:16:25Z, with the branch desktop OPEN on
  `workspace-3` (the same client as the failed attempt: backend at
  `8833fee3d2`, watchdog present, no stop-kind desktop code, the plugin
  subprocesses running the branch head -- so the desktop behaved as a
  pre-stop-kinds client, but the start was refused by the NEW plugin at once
  while the row was still `stopping`; a real 0.5.2 client's old plugin would
  wait out `stopping` and take the connector's 409 instead, which this drill
  did not exercise). The invocation was re-run unchanged
  against the FAILED record from 2026-09-13 on purpose, to exercise the
  resume: it skipped the harvest and replayed the 23:03Z latchkey harvest, and
  reused the published 0.5.2 image tar.
  - Row timeline (`psql` every 10 s): `leased` -> `stopping` with
    `stop_kind = maintenance` at 14:06:01Z -> `stopped` (maintenance,
    artifact generation 1) at 14:07:44Z -> parked (no box, no manifest, still
    maintenance) at 14:13:23Z -> `leased` on `21ae4720` at
    `15.204.52.75:22008/22009`, gen 2, `stop_kind` NULL at 14:16:28Z. The
    row never read `starting`.
  - Desktop (`minds.log`): HEALTHY -> STUCK at 14:06:14Z, `Unattended
    recovery ... DISPATCHED` at 14:06:15Z, and at 14:06:30Z the start step
    failed with `Error: This machine is undergoing maintenance and will be
    back shortly.` -- the connector's hold refused the watchdog's `mngr
    start`, one failed-step card as S10 accepts, no start; then
    `recovery_failed -> HEALTHY` at 14:16:49Z once the machine answered at
    its new address.
  - Latchkey supervisor (`events.jsonl`): two dials of the old VM endpoint
    at 14:16:44Z-46Z, then `Adopting the permissions ... from VPS
    15.204.52.75` + `Provisioned VPS-resident Latchkey gateway` at 14:17:04Z,
    `Detected host host-7696ff75... moving from 51.81.185.232:22003 to
    15.204.52.75:22009; re-resolving its latchkey gateway route and
    re-provisioning its VPS gateway` at 14:17:17Z (the #970 fix), and
    `Reverse tunnel established: remote 127.0.0.1:1988` at 14:17:21Z. No
    "Abandoning the credential store".
  - New VM: `latchkey-gateway` and `latchkey-tunnel` RUNNING,
    `/root/.latchkey/credentials.json.enc` and the other harvested files
    present, all four tmpfs secrets in `/run/mngr-latchkey`, both replay tars
    gone, the tunnel drop-in's `-p 2222` matching the container's published
    sshd port, 1989 and (after the desktop's pass) 1988 bound, the
    container's supervisord all RUNNING/EXITED, and `latchkey curl
    https://api.github.com/user` from inside the container answered the
    granted account (`joshalbrecht-agent`) without a re-grant. Report
    `migrate-20260914T141625Z.txt`: "latchkey state replayed and the gateway
    restarted" (plan FULL); the origin lima VM was destroyed; the record is
    RESTORED and the harvested keys shredded.
  - Not yet confirmed from the UI (Josh): the Permissions tab still showing
    GitHub, a permissions toggle working, and the failed-step card having
    cleared. Josh confirmed all three in the UI afterwards.
- **Drill 2 row**: one more `minds-v0.5.2` row baked on `c7793839` (hil,
  `US-WEST-OR`) 14:30Z-14:34Z from the box's cached 0.5.2 tar (row `069f56e1`,
  host `host-825db0910d1e4cd8a9dc01a6dd64aa7e`, agent
  `agent-4167258a4257454ab608698a97099c72`, container port 22003 -- the ports
  `workspace-3` vacated). Runbook checks: `DEFERRED_INSTALL_OK`,
  `system-services` STOPPED, git identity `minds-bootstrap`, `CONTENT_OK`,
  `git describe` = `minds-v0.5.2`. It is the tier's only available 0.5.2 row,
  to be claimed from the held-back 0.5.2 client before the upgraded-client
  migration drill.
  Josh's first claim (14:42Z, `workspace-4`) had in fact come from the branch
  client, whose create asks for `minds-v0.6.0`: it fast-path adopted 0.6.0 row
  `5c925d15` on the gen-2 beachhead (`15.204.52.75:22000/22001`), so it is an
  ordinary gen-2 workspace, not a migration candidate. A second 0.5.2 row was
  baked (15:03Z-15:07Z, row `78f1b93b`, host `host-824bc800b4ef4dabbd13ffd510bb1dd3`,
  ports 22004/22005, checks OK), the branch client was stopped and the real
  0.5.2 client launched from `/home/user/.mngr/worktrees-test/client-0-5-2`
  (its supervisor re-provisioned all four remote hosts, no "Abandoning"), and
  Josh claimed both rows from it: `workspace-migrate-in-old` (row `069f56e1`,
  15:06Z) and `workspace-migrate-in-new` (row `78f1b93b`, ~15:18Z), granting
  GitHub on each.
- **Drill 2a: `workspace-migrate-in-old` with the REAL 0.5.2 client open,
  PASSED** (15:20:21Z-15:31:28Z, fresh harvest, latchkey plan FULL, report
  `migrate-20260914T153127Z.txt`): the stop-kinds spec's step 2 as written.
  - Row: `stopping` (maintenance) 15:20:47Z -> `stopped` 15:22:30Z -> parked
    15:28:09Z -> `leased` on `21ae4720` at `15.204.52.75:22010/22011`, gen 2,
    kind NULL, 15:31:34Z; never `starting`.
  - The 0.5.2 desktop: STUCK 15:21:03Z, unattended recovery DISPATCHED; its
    old plugin waited out `stopping` and requested the start once the row read
    `stopped`; at 15:22:32Z the start failed with the connector's
    `409 {"detail":{"code":"workspace_under_maintenance","message":"This
    machine is undergoing maintenance and will be back shortly."}}` -- the
    server-side hold refusing a pre-stop-kinds client (S10). Josh then clicked
    the card's Restart at 15:26Z: the same 409, no start. A 0.5.2 card shows
    the raw JSON body rather than the sentence alone (its plugin predates the
    typed mapping); accepted. `recovery_failed -> HEALTHY` at 15:31:53Z once
    discovery re-resolved the new endpoints.
  - Machine side: `latchkey-gateway` and `latchkey-tunnel` RUNNING, the store
    and all four tmpfs secrets present, tars gone, `-p 2222` matching the
    container's sshd port, 1989 bound, and `latchkey curl
    https://api.github.com/user` from inside the container answering
    `joshalbrecht-agent` -- with NO desktop provisioning pass having reached
    the new VM (see next), so this is the latchkey leg proven on its own,
    the desktop-closed proof of L8 the earlier drill could not give.
  - The 0.5.2 supervisor (no #970 fix) kept dialing the old VM
    (`51.81.185.232:22002`, "Failed to expose the desktop Latchkey gateway on
    VPS port 1988") after the re-lease, so 1988 stayed unbound on the new VM
    and the desktop-forwarded routes need an app restart: the documented
    <= 0.6.0 limitation, observed as expected.
- Between the drills: `workspace-1` and `workspace-2` (the 2026-09-13 test
  workspaces) were destroyed by Josh from the UI to make room on the beachhead
  (6 slots; a first attempt at drill 2b was refused by the migrate's soft
  capacity check before touching the workspace: "target box fits an estimated
  6 machine(s) and 6 are in use"). A share-link display bug found on the way
  is filed as imbue-ai/mngr-internal#1012 (pre-existing, not this branch).
- **Drill 2b: `workspace-migrate-in-new` with the HEAD client open, PASSED**
  (client from `6dd9f95fb0` with the stop-kind desktop code; migrate
  16:06:27Z-16:18:07Z, fresh harvest, plan FULL, report
  `migrate-20260914T161807Z.txt`): the stop-kinds spec's step 1.
  - Row: `stopping` (maintenance) 16:06:50Z -> `stopped` 16:08:33Z -> parked
    16:14:33Z -> `leased` on `21ae4720` at `15.204.52.75:22004/22005`, gen 2,
    kind NULL, 16:18:09Z.
  - Desktop: STUCK at 16:07:07Z; the recovery gate's live connector read
    declined at 16:07:11Z ("Not auto-starting ...: its stop was requested
    (the connector reports it stopping)", "Suppressed unattended recovery
    ... (stopped on purpose)"); NO dispatch, no `mngr start`, no failure
    card; `stuck -> HEALTHY` at 16:18:42Z once the machine answered at its
    new address. (Josh watched the UI for the Maintenance badge and band.)
  - Supervisor (#970 fix): two dials of the old endpoint at 16:18:39Z-41Z,
    `Detected host ... moving from 51.81.185.232:22005 to 15.204.52.75:22005`
    at 16:19:12Z, reverse tunnel on the new VM at 16:19:16Z, both agents'
    gateways provisioned; no "Abandoning".
  - Machine side: gateway and tunnel RUNNING, store and four tmpfs secrets
    present, tars gone, `-p 2222` matching, 1988 and 1989 bound, container
    supervisord clean, `latchkey curl https://api.github.com/user` from inside
    the container answering `joshalbrecht-agent` without a re-grant.

## Deferred, deliberately

- The re-test of the old-client migration with the latchkey fix and the
  stop-kind fix (a fresh 0.5.2 workspace holding a granted credential; the
  desktop-open case per the stop-kinds spec's test plan, and the desktop-closed
  case per the latchkey spec's section 7), then the rest of staging.
- Production gen-2 bring-up beyond the committed `[management_plane]`: its
  `[ssh_ca]` (needs the `minds_production` Vault role), the connector AppRole,
  the connector deploy, and a fresh gen-2 box. The bake guard refuses
  `minds-v0.6.0` on gen-1 boxes, so production cannot hold 0.6.0 rows until then.
- The production services deploy, the pool bake there, and every channel
  promotion.
