# minds-v0.6.1 (2026-09-14/15): the second gen-2 release, cut and built; staging rehearsed and fully cut over to gen-2

Tag pair: mngr `0c9d81e7f6` (reachable on `main` as the second parent of merge
`f0229ba36d`, PR #1032), default-workspace-template `a87c68e19` (DWT PR #595's
merge into `main`), both `minds-v0.6.1`, annotated; vendor-match verified
against DWT `origin/main` after the merge (`system/vendor/mngr` equals
`git archive` of the mngr SHA modulo `.minds/`). ToDesktop build
`260915wjcyd06bp` (pre-merge launch-to-msg run 34912494056 on the frozen pair
`0c9d81e7f6` / DWT `b9bed4e23`, a real 40 minute run with `build`,
`macos_launch`, `launch_to_msg` and `notify_slack` all green; the tag run is
recorded below).

Cut from `main` one day after `minds-v0.6.0`, carrying the ~600 commits that
landed in between: the workspace stop kinds and the migrate's latchkey-state
replay (both drilled on staging before the cut; see
[minds-v0.6.0.md](./minds-v0.6.0.md)), the chat-agent refactor, proactive
compaction, the new-tab chat tile, the latchkey 3.13.0 bump, the remote
workspace fixes and the connector's web-pin read from the release feed. The
cut itself deployed nothing; the "Staging tier (2026-09-15)" section below
records the staging deploy, the 0.6.1 bakes, and the migration of every
remaining staging workspace onto gen-2 that followed the same night. **Nothing
was deployed to production and no channel moved.**

## What rode along in the bump commit

- Version 0.6.1, `FALLBACK_BRANCH = minds-v0.6.1`, and the 0.4.0 wire-compat
  snapshot's window extended to 2026-10-14: the only `wire_types.py` change
  since `minds-v0.6.0` is the optional `stop_kind` field on the workspace entry
  and the `WorkspaceStopKind` enum it carries, so no new snapshot was needed.
- **A real bug on `main`, found while establishing state:** every ToDesktop
  build since the latchkey 3.13.0 bump (`71612b28ad`, 2026-09-14) failed at
  "Configuring App" with `pnpm install --prod=false` exiting 1 on both the Mac
  and Linux targets (run 34901210332 on `main` `f017239585`). latchkey pins
  playwright to an exact version, 3.13.0 pinned one published inside
  `pnpm-workspace.yaml`'s 14-day `minimumReleaseAge` cooldown, and ToDesktop's
  agents install with `--no-frozen-lockfile`, which re-resolves from the
  registry and refuses it; CI's frozen installs never noticed. The fix
  (`playwright` and `playwright-core` added to `minimumReleaseAgeExclude`,
  first written on the unmerged `mngr/linux-package-testing` branch, PR #968)
  was cherry-picked into the bump commit and proven locally: deleting the
  lockfile and running `pnpm install --lockfile-only` fails without the
  exemption and passes with it. Without this, no 0.6.x build was possible
  until the cooldown expired around 2026-09-19.
- Debian trixie guest-image pins (DWT `[providers.lima]` and
  `mirror_artifacts.py`, both `20260722-2547`) and the apt snapshot timestamps
  (`20260725T000000Z` on both sides) were checked and needed no bump, so
  step 0 did not apply. No tier configures `lima_image_base_url`, so 8b did
  not apply either.

## Verification

- mngr PR #1032 CI (run 34912440779): every job green, including
  `test-offload`, `test-offload-acceptance`, `test-docker` and
  `test-minds-snapshot`.
- The minds release dispatch (`run_minds_release_tests=true`, run 34912507972,
  `template_ref` = DWT `b9bed4e23`, one hour end to end): `test-minds-release`
  green with 3 `minds_deployment` tests passed and 5 plain release tests
  passed / 1 skipped (the AWS opt-in); `test-minds-snapshot` green with 24
  `minds_services` passed / 3 skipped (the relay tests skip by design on a
  per-run env). `build-minds-ci-env`, `warm-minds-pool-cache` and
  `destroy-minds-ci-env` all green.
- DWT PR #595 CI: green on the second run (34912828885) after a changelog
  entry was added. The first run failed twice: the `check-changelog` gate
  (the vendor refresh counts as the synthetic `dev` project and needs
  `system/changelog/<branch>.md`, as PR #584 had), and
  `system/scripts/agy_shim/agy_shim_test.py::test_the_reminder_returns_on_the_next_turn`,
  a dwt-native test whose own comment says the kernel may hand a recreated
  marker the same inode; it fired the `[Step tracking reminder]` instead of
  the `Open task reminder` it asserts on. Not caused by the vendor refresh
  (the strings are dwt scripts, and `main` was green on the same base); it
  passed on the rerun. Worth marking flaky or hardening in DWT.
- Tag run: launch-to-msg run 34916838276 on `commit_sha=minds-v0.6.1` /
  `template_ref=minds-v0.6.1`, green in 16 minutes: `build` reused
  `260915wjcyd06bp` for the identical mngr SHA (by design), and
  `launch_to_msg` was a real 15 minute round-trip through the binary's baked
  `FALLBACK_BRANCH`, not a marker-cache skip. Green here concludes the
  artifacts.

## Staging tier (2026-09-15): the 0.6.1 rehearsal and the full gen-2 cutover

Everything below was run from the `mngr/deploy-0-6-1` worktree (`main` at
`63eb36a470` plus this branch's commits) with Josh away; owners were not
notified (staging, overnight). Findings and the tooling fixes they led to are
listed at the end; each fix is its own commit on this branch.

- **Services deploy** from this tree: deploy_id `20260915T021647Z`, ROLLOVER
  (no pending migrations; schema stays at 042), 02:16Z-02:2xZ, both health
  checks green, Neon snapshot deleted. Ships the Modal `us` region pin
  (`dcf4b7075c`), the release-channel web pin code (`35f1c09fbd`) and the 0.6.1
  bump: the staging web create pin is now `minds-v0.6.1`. Verified: `/version`
  advanced, and `modal container exec ... 'echo $MODAL_REGION'` answers
  `us-west-2` for both the connector and the proxy containers (the previous
  deploy had landed in `spaincentral`). The deploy ran BEFORE the bake, on
  Josh's instruction; the runbooks that said the opposite for staging were
  corrected (`bf30e27da5`).
- **0.6.1 bake on the beachhead `21ae4720`**: one row (`54918d5e`,
  `host-db5acae7...`, ports 22006/22007), fresh seed (the box held only the
  0.5.2 tar), verified over the operator certificate: `DEFERRED_INSTALL_OK`,
  `system-services` STOPPED, git identity `minds-bootstrap`, `CONTENT_OK`,
  gVisor kernel, vendored version 0.6.1. Josh claimed it from the 0.6.1
  desktop (`new-1`) and reported it working, then destroyed his four other
  gen-2 workspaces (`workspace-3`, `workspace-4`, `workspace-migrate-in-old`,
  `workspace-migrate-in-new`) from the UI.
- **Housekeeping**: the 11 never-leased old-tag `available` gen-1 rows (7 on
  the vin box, 4 on `c7793839`) and the spare 0.6.0 row `ee7b2cab` destroyed
  (12/12); hynek's tagless `08bb40d7` and the three drill-era stopped rows
  (hynek's `5d92180c` and `bde4246c` at 0.3.10, qi's `076ee551` at 0.3.11)
  released on Josh's decision; the five RESTORED cutover records of destroyed
  workspaces archived under `~/.minds-staging/cutover/workspaces/archive/`.
- **hil sweep onto the beachhead** (`--source-server-id c7793839`): mark
  `7032a4ea` (0.4.2, slow-path row), gabriel `6387169d` (0.4.2), weishi
  `6a593eca` (0.5.0). Four runs were needed; the first three each found a
  tooling bug (F2, F3, F4 below), mark's row spent ~1 h parked and was rolled
  back once (`cutover rollback`, 04:29Z-04:32Z: back on gen-1 at
  `51.81.185.232:22000`, its home tree intact) before its layout was
  repaired and it migrated from scratch. Final run (04:34Z-05:22Z):
  - mark: stopping 04:34:42Z -> stopped 04:37:16Z -> leased on `21ae4720` at
    `15.204.52.75:22000/22001` 04:53:02Z (18 min; latchkey DISK_ONLY since
    the rollback's fresh VM had no tmpfs pair: the gateway starts at his
    desktop's next provisioning pass).
  - gabriel: 04:53:12Z -> stopped 04:54:35Z -> leased at 22002/22003
    05:03:59Z (10.8 min; FULL latchkey plan, gateway + tunnel RUNNING, :1989
    bound).
  - weishi: 0.5.0 seed 05:04Z-05:12Z (before her stop, see F2b), stopping
    05:12:23Z -> stopped 05:13:55Z -> leased at 22004/22005 ~05:22Z (10 min
    after the seed; FULL plan).
  Each verified on its VM: 12-14 container programs RUNNING, system_interface
  200, `/home/user -> /mngr-vol/home` with the checkout (1.2 / 1.1 / 0.8 GB).
- **Stopped rows** (the admin-start path): weishi `72aa6935` (0.5.1) started
  onto `c7793839` in 1m43s (05:24:34Z -> 05:26:17Z), 0.5.1 seeded, stopping
  05:35:21Z -> stopped 05:36:44Z -> parked 05:42:33Z -> leased at 22008/22009
  05:46:09Z. russ `3a624083` (0.4.2 by `git describe`; the row said 0.4.1):
  admin-started, refused at the harvest by the new layout guard (F4) with the
  workspace left running, repaired, re-run, refused by the disk-stamp check
  (F7), fixed, re-run: restamped 44 -> 45, stopping 05:54:23Z -> stopped
  05:56:57Z -> parked 06:04:29Z -> leased at 22010/22011 06:12:42Z; 13
  programs RUNNING, shell 200, data disk 45 GB. Beachhead 6/6.
- **Repave `c7793839`** (gen-1 hil, rowless): 06:14Z-06:31Z, `REPAVED`,
  gen-2 `ready`, storage partition 936 GB, overlay `10.96.1.2`; audit keys
  0/0, CA correct, encrypted, 3 exclusive / 0 contaminated. Sizing 120 units,
  808 GiB budget, 14 slots. One 0.6.1 row baked on it 06:33Z-06:49Z
  (`host-ccac8bc7...`, ports 22000/22001), all runbook checks OK.
- **Layout repairs** (`minds-admin repair-home-layout --migrate`, on gen-1
  before each migrate): hynek `workspace-3` (1.01 GB, 04:32Z), mark
  `workspace-1` (1.36 GB, 04:33Z), russ `workspace-1` (1.35 GB, 05:49Z). Each
  keeps a rollback snapshot under `<volume>/rollback/` and the aside copy at
  `/home/user.pre-migration` in the container.
- **vin sweep, cross-region** (`--source-server-id 72dd8187` onto `c7793839`,
  Josh-approved): hynek `9e9bacd1` (0.5.2, tar already in the bucket):
  stopping 06:43:02Z -> stopped 06:45:06Z -> parked 06:52:58Z -> leased at
  `51.81.185.232:22002/22003` 07:00:30Z (17.5 min); `665c9b2a` (0.6.0; its
  tar seeded on the target before the stop): stopping 07:08:13Z -> stopped
  07:10:26Z -> parked 07:18:19Z -> leased at 22004/22005 07:25:51Z. Both
  FULL latchkey plans, gateway + tunnel RUNNING, 14 programs RUNNING, shell
  200. Their rows keep `region = US-EAST-VA`, which is what moves them home:
  once the vin box is gen-2 a stop and start restores them there.
- **Every staging workspace is on gen-2 as of 07:26Z** (eight migrated today
  plus Josh's `new-1`); no gen-1 row remains in any status.
- **Gen-2 stop/start test** (gabriel, operator stop with the `maintenance`
  hold at 07:27:13Z, stopped 07:28:53Z, retention finalize 07:37:26Z, admin
  start 07:37:22Z): restored onto the *other* hil gen-2 box `c7793839` at
  22006/22007 in 1m39s, container healthy, latchkey store intact; the VM's
  gateway sits FATAL until his desktop next provisions the machine (no tmpfs
  pair on a fresh VM), the tunnel RUNNING -- the documented post-restore state.
- **Repave `72dd8187`** (gen-1 vin, rowless): 07:27Z-07:45Z, `REPAVED`,
  gen-2 `ready`, storage partition 936 GB, overlay `10.96.1.3`; fleet audit
  afterwards: all three boxes gen-2, keys 0/0, CA correct, encrypted, 3
  exclusive / 0 contaminated. One 0.6.1 row baked on it (US-EAST-VA).
- **hynek's move home** (operator stop with the `maintenance` hold on both
  at 07:46Z, retention finalize ~07:56Z, admin start): `665c9b2a` stopped
  07:48:24Z -> leased on the vin box `72dd8187` at `135.148.34.235:22002/22003`
  07:57:29Z; `9e9bacd1` stopped 07:48:44Z -> leased at 22004/22005 07:58:40Z.
  Each restore took about a minute; the connector chose vin on its own because
  the rows never left `region = US-EAST-VA`. Both containers healthy (14
  programs RUNNING, shell 200, checkouts intact, latchkey stores carried);
  manu-delago's VM gateway was already RUNNING with its tmpfs pair (a desktop
  provisioning pass reached it within the minute), workspace-3's sits FATAL
  until the next pass, as after every gen-2 restore.
- **End state (08:00Z)**: boxes `21ae4720` (hil), `c7793839` (hil) and
  `72dd8187` (vin) all gen-2 `ready`; 0 gen-1 rows in any status; 9 leased
  gen-2 workspaces (Josh, mark, weishi x2, russ on the beachhead; gabriel on
  `c7793839`; hynek x2 on vin) and one `available` 0.6.1 row on `c7793839`
  and on `72dd8187` (the beachhead's went to Josh's `new-1`). Owners still on
  0.6.0-or-older desktops need one app restart for the desktop-forwarded
  latchkey routes; their workspaces and GitHub keep working meanwhile.

### Findings (each fix is its own commit on `mngr/deploy-0-6-1`)

- **F1, fixed `b5c22dbf7b`**: a workspace created from a branch has no
  `minds-v*` tag (the desktop's single-branch clone fetches heads only), so
  the version probe found nothing; it now falls back to the vendored
  `FALLBACK_BRANCH`. hynek's `08bb40d7` was the case (released instead).
- **F2, fixed `e49410ee8b`**: the image seed bakes the template at the
  workspace's tag with today's mngr; tags before 0.5.0 still name
  `auto_dismiss_dialogs` (renamed in `a50d2a8b90`), so the strict settings
  parse failed the seed three times and left mark parked. The seed bake alone
  now runs with `MNGR_ALLOW_UNKNOWN_CONFIG=1`.
- **F2b, fixed `f5f503974a`**: the seed ran after the stop and park; it now
  runs right after the harvest, so an unbuildable version fails with the
  workspace still running.
- **F3, fixed `84a14bb466`**: pre-0.5.0 templates create a boot chat, which
  the pool bake's primary-agent gate refuses, so the seed failed three full
  carves after its first create had already saved the tar. A seed-only bake
  now tears its slice down as soon as the create returns.
- **F4, fixed `266944eb3b`** (data safety): the migrate transplants the data
  disk only; a slow-path-rebuilt workspace on the legacy volume layout keeps
  `/home/user` in the container's writable layer and came back EMPTY (mark's
  first attempt; rolled back, nothing lost). The preflight and the harvest now
  probe the layout and refuse anything but `home/`, naming
  `repair-home-layout --migrate` as the remedy. Three of staging's eight
  workspaces were legacy (mark, russ, hynek's workspace-3).
- **F5, explained by F4**: the VM tunnel key is authorized in the container
  ssh user's `$HOME/.ssh/authorized_keys`, and these containers run root with
  `HOME=/home/user`, so it lives in the home tree: lost with a legacy layout,
  carried on `home/`.
- **F6, fixed `cbf87098af`**: `cutover migrate --workspace` took a row-id
  prefix to a raw psycopg2 error; it is a click UUID now.
- **F7, fixed `99350b0a4c`**: migration 039 stamped `disk_gb = 44` on every
  gen-1 row it could not measure (stopped on no box at the time); the harvest
  refused the mismatch with the measured disk. It now restamps such a row from
  the measurement (pinned to the fallback value) and the preflight warns.
  Production will have this class of rows.
- **F8, observation**: after any gen-2 restore the VM's `latchkey-gateway` is
  FATAL until the owner's desktop next provisions the machine (a fresh VM has
  no tmpfs key/password) while the tunnel runs; the store survives.
- **F2c, fixed after the sweep**: a `--source-server-id` sweep did not
  re-select a row it parked earlier (the park clears the row's box link, so
  the box's row listing misses it), so a resume needed `--workspace` for it
  (mark's `7032a4ea`). The sweep now also selects every in-flight state record
  whose origin is the swept box and whose target is this invocation's, and
  warns about records parked onto another target.
- **Not fixed**: the bake retries a deterministic config error three times
  with a fresh slice each (F2a); the runbooks said bake-before-deploy for
  staging (fixed in docs, `bf30e27da5`).

### Timings observed today

Stop to `stopped`: 1.4 to 2.6 min. Park to re-lease (S3 rollback copy,
transplant, replay, health): 8 to 16 min. Whole migration with the tar in the
bucket: 10 to 18 min; a first-of-version seed adds 8 to 15 min. Gen-1
admin-start restore: 1m43s. Gen-2 stop: 1m40s; gen-2 cross-box restore: 1m39s.
Repave: 17 to 22 min. Fresh 0.6.1 bake: 15 to 16 min.

## Production tier (2026-09-15): services deployed

Run from the `mngr/production-ssh-ca` branch (`main` at `af0b23b973`, the
merge of PR #1043, plus `114214b880`, the production `[ssh_ca]` commit), with
Josh present. Steps 2 and 3 of `next_deploy.md`'s production plan; boxes,
bakes, migrations and channels had not started when this was written.

- **SSH CA**: `[ssh_ca] public_key` committed for production (read from
  `minds-production-ssh/config/ca`); the pinned loader test that kept the
  tier CA-less removed. The connector AppRole `minds-connector-production`'s
  role-id and a fresh secret-id minted into `secrets/minds/production/ssh-ca`.
- **Services deploy**: deploy_id `20260915T154933Z`, RECREATE, 15:49Z-15:5xZ.
  Applied pool-hosts migrations 034 through 042 (schema was at 033: the
  2026-09-09 deploy `20260909T225715Z`, recorded nowhere, had not carried
  them), pushed every per-env Modal Secret including the new
  `ssh-ca-production`, deployed `llm-production` (Prisma migration clean),
  `rsc-production` (custom domains `accounts.imbue.com` and
  `minds.imbue.com`, Modal proxy `mind-connector-east` attached) and
  `analytics-production` (no pending ops migrations); both health checks
  green; Neon snapshot deleted; recover-target file removed.
- **Verified**: `/version` advanced to `20260915T154933Z`, liveness ok, the
  three web channel files still name `minds-v0.5.2` (30 `available` rows in
  US-EAST-VA, 17 in US-WEST-OR at that tag), `modal container exec ... 'echo
  $MODAL_REGION'` answers `westus2` / `westus3` / `us-central1` for the four
  live `rsc-production` containers (all US; the broad `region="us"` pin spans
  the three clouds' US regions, not only AWS `us-*` names), and
  `minds-admin server list` reads the fleet again (18 gen-1 boxes: 17
  `24sys032-us` and one `24sk602-v1-us`, all `ready`).
- **First production gen-2 boxes** (16:0xZ-16:39Z, Josh's choice: one fresh
  box per region rather than a drain-and-repave; the standard `24sys03-v1-us`
  with 2x960 NVMe was not orderable in hil, so hil got the same plan with
  2x1920 NVMe; `24sys052-us` was considered and rejected for its SATA-only
  storage and the untested two-disk-group reinstall layout). Both orders
  placed 16:0xZ, delivered within about 15 minutes, set up in about 12
  minutes each:
  - `8797d3ae-f77b-4a40-8b98-91e22ab98913` vin / US-EAST-VA, OVH order
    8870934, `ns1011325.ip-135-148-169.us` (135.148.169.102), 128 GB,
    2x960 NVMe, $160 due now ($100/mo), overlay 10.64.1.1, 14 slots.
  - `3841df6d-a749-4f6b-988e-c7cb12b13589` hil / US-WEST-OR, OVH order
    8870936, `ns107170.ip-147-135-97.us` (147.135.97.227), 128 GB, 2x1920
    NVMe, $224 due now ($164/mo), overlay 10.64.1.2, 14 slots.
  Each setup minted the operator's production management certificate
  (identity created at `~/.mindsadmin/production/ssh_id`), recorded a fresh
  LUKS recovery passphrase in Vault and uploaded the header backup to the
  bucket, installed the observability collector, and landed the `:22`
  lockdown. `just server-audit` afterwards: both boxes keys 0/0, CA correct,
  encrypted, exclusive to the tier, no degraded arrays; the audit reached
  them over the operator tunnel, so the lockdown and WireGuard path work
  from the operator machine. The artifact mirror verified clean (14 present)
  before the first prep. No row baked yet.
- **First production 0.6.1 bakes** (17:0xZ-17:4xZ, one row per new box,
  first-of-version seed on each, concurrent): vin `8797d3ae` row `4ef0efdb`
  (`host-bd34706b923a4749a061a7c418b998a4`, ports 22000/22001); hil
  `3841df6d` row `8697d85a` (`host-7cfbdbabc8894e73b0c3c24e08740378`, ports
  22000/22001). Both `available` at `minds-v0.6.1`, 8 units / 44 GB, runsc.
  Verified over the operator certificate with the runbook script:
  `DEFERRED_INSTALL_OK`, `system-services` STOPPED, git identity
  `minds-bootstrap`, `CONTENT_OK`, gVisor kernel, vendored version 0.6.1 on
  both. Not yet leased from a desktop.
- **Second box per region ordered** (17:2xZ, Josh: spare capacity): vin
  `f763270e-a0b1-4c33-8d44-30a0e6c37263` (OVH order 8871197, $160) and hil
  `149ac508-e492-4d83-9d3f-45fe9b771550` (OVH order 8871205, $224), same
  configurations as the first pair. Delivered 17:38Z (both), set up gen-2
  concurrently: vin `ns1011321.ip-135-148-169.us` (135.148.169.103) READY
  17:5xZ, hil `ns1000320.ip-51-81-242.us` (51.81.242.68) READY 18:0xZ, 14
  slots each, no rows baked on them yet. Fleet audit afterwards: all four
  gen-2 boxes keys 0/0, CA correct, encrypted, exclusive; 27 servers,
  294/393 slots used.
- **Three more 0.6.1 rows per new box** (17:2xZ-17:5xZ, tar already on each
  box, three concurrent carves per box, 3/3 succeeded on each): vin ports
  22002/22003, 22004/22005, 22006/22007; hil the same ports. All six verified
  with the runbook script (DEFERRED_INSTALL_OK, `system-services` STOPPED,
  `minds-bootstrap`, CONTENT_OK, 0.6.1). Four `available` 0.6.1 rows per
  region.
- **Pre-0.3.10 workspaces are a different shape.** The first production
  preflight (Josh's four boxes, 55 rows) found 19 rows baked at 0.3.5-0.3.8
  with an empty version probe: those containers run as root with
  `HOME=/root`, keep the template checkout at `/mngr-vol/host_dir/code`
  (no `/home/user/workspace`, no vendored mngr, `git describe` there reads
  their bake tag plus a few own commits), and are exactly the cohort the
  0.3.10 floor excludes. Fleet-wide, 41 rows were baked below 0.3.10. The
  probe should name this layout and report "below the floor" instead of a
  parse error (follow-up). One 0.5.x row on the same boxes has the legacy
  home layout (`repair-home-layout` first); the 7 `available` 0.5.2 rows
  there are destroyable before a repave.
- **First production migration** (Josh's `test-of-0-4-3`, row `3a536d11`,
  0.4.3, `home/` layout, latchkey set up by Josh beforehand; the desktop and
  its supervisor closed for the migration): `cutover migrate
  --yes-i-mean-production --target-server-id 3841df6d --workspace 3a536d11`,
  18:03Z-18:2xZ. Harvest FULL (8 disk files, 2 confs, 4 tmpfs secrets);
  0.4.3 image seeded on the hil gen-2 box before the stop (seed-only slice,
  torn down); maintenance stop, rollback copy, park, transplant, replay,
  re-lease at `147.135.97.227:22008/22009`, disk 45 GB, gen-2, `stop_kind`
  cleared. Verified on the VM: `latchkey-gateway` and `latchkey-tunnel`
  RUNNING, all four tmpfs secrets and the store present, `:1989` bound;
  in the container: 13 programs RUNNING, system_interface 200, `/home/user ->
  /mngr-vol/home` with the 793 MB checkout at `minds-v0.4.3-3-g…`, gVisor.
  Desktop relaunched afterwards: its health tracker saw the old port fail,
  dispatched one unattended start-only recovery (a no-op on the leased row),
  re-established the forwards at the new address within 25 s, adopted the
  machine's permissions and provisioned the new VM's gateway within a minute.
- **Fleet inventory at this point** (pool DB, SuperTokens for emails): 274
  owned gen-1 rows across 127 owners (36 imbue.com owners hold 141, 91
  external owners hold 133); US-WEST-OR 189 leased + 25 stopped, US-EAST-VA
  47 leased + 13 stopped; all 38 stopped rows finalized on no box. Versions:
  141 rows at 0.4.x, 92 at 0.3.x (41 of them below the 0.3.10 migrate
  floor), 41 at 0.5.x. Release channels are not recorded server-side; the
  connector access log's `X-Imbue-Client` header gives each active user's
  desktop version instead (14 days: 28 on 0.4.2, 23 on 0.5.2, 16 on 0.5.0,
  3 on 0.4.1, 3 on 0.6.x, 26 with no desktop header; 47 owners not seen).
- **Migration 039 stamped `disk_gb = 44` on 38 gen-1 rows**, exactly the 38
  `stopped` rows on no box at deploy time (324 gen-1 rows in all); the
  migrate's F7 restamp covers them.

- **First production box emptied and repaved: `462c56c0` (vin)** (Josh's
  choice of a single box to prove the sequence; migrations stay within a
  region, vin -> vin and hil -> hil, on his instruction). daniel@imbue.com's
  `workspace-2` (row `93f13634`, `host-b4fe7c1c…`, 0.3.17, `home/` layout,
  latchkey DISK_ONLY: 7 disk files, 2 supervisor confs, no tmpfs pair on the
  origin) migrated with `cutover migrate --yes-i-mean-production
  --target-server-id 8797d3ae --workspace 93f13634-…`, 20:17Z-20:37Z
  (report `migrate-20260915T203731Z`): first-of-version 0.3.17 seed on the
  vin gen-2 box before the stop (tolerant config path, seed slice torn down,
  tar published to `cutover/images/`), maintenance stop, rollback copy,
  park, transplant, replay, re-lease at `135.148.169.102:22008/22009` (gen-2,
  8 units, 45 GB, region `US-EAST-VA` unchanged), origin VM destroyed.
  Verified on the VM: container under runsc, 14 in-container programs
  RUNNING/EXITED, `/home/user -> /mngr-vol/home` with the 1.1 GB checkout at
  `minds-v0.3.17-2-g…`, system_interface 200, latchkey files replayed with
  no tmpfs secrets (the gateway starts at daniel's next desktop provisioning
  pass; he is on a 0.4.2 desktop, so one app restart is needed). The box then
  held zero rows; `cutover repave --yes-i-mean-production --server-id
  462c56c0-…` ran 20:38Z-20:51Z (report `repave-20260915T205114Z`): gen-2
  `ready`, 14 slots, overlay `10.64.1.4`, storage partition 936 GB, LUKS
  header backed up; the fleet audit afterwards read 27 boxes, 27 exclusive,
  0 contaminated, keys 0/0 and CA correct on the new box. No row baked on it
  yet.
- **Overlay address collision found by that audit's follow-up:** the second
  gen-2 pair, set up concurrently at 17:38Z, both carry `wireguard_address =
  10.64.1.3` (`f763270e` vin and `149ac508` hil; each setup log says
  "Assigned management overlay address 10.64.1.3"). `_ensure_box_wireguard_address`
  in `cli/server.py` reads the assigned set and stamps the next free address
  without a lock or a unique constraint, so two concurrent setups pick the
  same one. The userspace onetun dial builds one tunnel per box from the
  box's own key and endpoint, so operator commands still reach both (the
  audit did); the rendered kernel-route operator config (`minds-admin
  wireguard config`) would list two peers with the same `AllowedIPs` and
  reach only one of them, and the connector never uses the overlay address.
  Repaired 00:2xZ (2026-09-16) on `149ac508` (hil, empty): a first
  `server prep` after restamping the row to `10.64.1.5` failed before
  touching the box (the operator tunnel is built to the row's address, which
  the box's wg0 did not carry yet, so the dial fell back to the locked-down
  public `:22`), so the order that works is: row back to the box's current
  address, `server ssh` to edit `Address` in `/etc/wireguard/wg0.conf` to
  the new one and restart `wg-quick@wg0` from a detached `systemd-run`
  timer, then the pinned restamp to `10.64.1.5`, then `server prep`
  (converged in under a minute, WireGuard up with the same box key). The
  duplicate query is empty, the box reads `10.64.1.5/11` on wg0, and the
  audit is clean (27 exclusive). Tooling fix on this branch: the allocation
  now runs under a transaction-scoped advisory lock in one transaction
  (`allocate_box_wireguard_address`), and connector migration 043 adds a
  partial unique index on `wireguard_address` (queued in `next_deploy.md`;
  it refuses to apply on a tier still carrying a duplicate).

- **Third box per region ordered and set up as migration buffer** (2026-09-16
  00:5xZ-01:20Z, Josh: hold them empty rather than bake them): vin
  `2b5cc1f7-38f7-413c-9d49-fdac1cc19f81` (OVH order 8872507, $160,
  `ns1011322.ip-135-148-169.us` 135.148.169.104, overlay 10.64.1.6) and hil
  `5f099257-3841-4665-ae2e-1ca906d9b564` (OVH order 8872509, $224,
  `ns1000324.ip-51-81-242.us` 51.81.242.72, overlay 10.64.1.7), same
  configurations as the earlier pairs, delivered in about 20 minutes, set up
  concurrently through the locked allocation (distinct addresses, the first
  live exercise of the fix above). Seven gen-2 boxes now: four vin, three hil.
- **0.6.1 stock for the alpha/beta promotion** (01:55Z-01:27Z): 14 rows baked
  on the repaved vin box `462c56c0` (`just pool-bake US-EAST-VA minds-v0.6.1
  14 --server-id 462c56c0-…`, seed then fill, 14/14, ports 22000-22027) and 14
  on hil `149ac508` (same, 14/14, ports 22000-22027), about 30 minutes each
  with a first-of-tag seed on both boxes. All 28 verified with the runbook
  script (`DEFERRED_INSTALL_OK`, `system-services` STOPPED, `minds-bootstrap`,
  `CONTENT_OK`). The pool then held 18 `available` 0.6.1 rows in US-EAST-VA
  and 17 in US-WEST-OR; the 0.5.2 gen-1 rows stay for stable.
- **Promoted to beta and alpha, desktop and web** (PR #1057 from `main`,
  merged 01:29:49Z; the release-channels run published within a minute):
  `[channels.beta]` and `[channels.alpha]` at build `260915wjcyd06bp`
  (0.6.1, 100%), `[web_channels.beta]` and `[web_channels.alpha]` at
  `minds-v0.6.1`. Confirmed on the feed: `beta-mac.yml` and `alpha-mac.yml`
  serve 0.6.1 at `stagingPercentage: 100`, `beta-web.json` and
  `alpha-web.json` pin `minds-v0.6.1`; stable stays at 0.5.2 (desktop and
  web), so the connector download fallback is untouched. Josh's plan: get
  people onto 0.6.1 through these channels before the remaining box sweeps.

- **Promoted to stable, desktop and web** (2026-09-16, PR #1076 from
  `main`, merged 17:09:33Z; published within a minute): `[channels.stable]`
  at build `260915wjcyd06bp` (0.6.1, 100%, Josh's call rather than a ramp)
  and `[web_channels.stable]` at `minds-v0.6.1`; the connector's download
  fallback (`_DEFAULT_TARGET_BY_PLATFORM`) bumped to the same build in the
  same PR (it reaches production at the next connector deploy). Confirmed on
  the feed: `stable-mac.yml` serves 0.6.1 at `stagingPercentage: 100` with
  the `260915wjcyd06bp` arm64 dmg, `stable-web.json` pins `minds-v0.6.1`,
  and `minds.imbue.com/download?platform=mac-arm64` redirects to that dmg.
  Every channel now serves 0.6.1; the 0.5.2 gen-1 `available` rows are no
  longer named by any web pin and can be retired when the gen-1 boxes are
  swept.
- **Two more hil boxes as migration buffer** (2026-09-16 16:5xZ-, same
  `24sys03-v1-us` 2x1920 NVMe configuration, $224 each): `95bbc37c-7702-4d52-b970-e15ed09300b8`
  (OVH order 8885505, `ns1000199.ip-147-135-97.us` 147.135.97.118, overlay
  10.64.1.8) and `0172f93e-6d3f-4e0e-840d-73282aa8967e` (OVH order 8885509,
  `ns1002929.ip-51-81-243.us` 51.81.243.47, overlay 10.64.1.9), delivered in
  about 15 minutes, set up concurrently (distinct addresses again). The first
  setup of `95bbc37c` died on a transient `deb.debian.org` fetch (OpenSSL
  broken pipe, IPv6) during the prep; a plain `server setup` re-run resumed
  it from `installing`. Both READY by 17:23Z (LUKS headers backed up, 14 slots each, left empty as buffer); no duplicate overlay address, and the fleet audit reads 31 boxes, 31 exclusive, 0 contaminated. Nine gen-2 boxes now: four vin, five hil.
## Deferred, deliberately (Josh)

- The production gen-2 bring-up still listed in [../next_deploy.md](../next_deploy.md),
  the production services deploy, the production bake, and every channel
  promotion (desktop and web). Beta and stable remain at 0.5.2 build
  `260909wnd3gb4z1`; alpha too, since 0.6.0 was never promoted.
- The `agy_shim` inode flake above, in the DWT repo.
