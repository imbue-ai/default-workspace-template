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

- [ ] **Ship the digest-pinned workspace base image** (imbue-ai/mngr-internal#1138;
  DWT PR #634). Docker Hub moved the unpinned `python:3.12-slim-trixie` tag to
  a Debian 13.7 build on 2026-09-19, past the `20260725T000000Z` snapshot every
  0.6.x template pins, so every fresh image build failed: first-of-tag seed
  bakes, new desktop Docker creates, and CI's `build-minds-snapshot` job. The
  fix pins the 13.6 index digest (`57cd7c3a…`, whose packages are exactly the
  snapshot's -- no version change for any workspace) and is already on DWT
  `main`-bound PR #634; the next tag cut carries it, and step 0 now bumps the
  digest together with the timestamp. **A new release is not required for
  this.** First-of-tag seeds of `minds-v0.6.2` (and of every older tag the
  gen-2 migration bakes) work again from the operator side: the bake builds
  a floating `FROM` against the digest recorded for its snapshot
  (imbue-ai/mngr-internal#1143), so the tar-copy workaround is retired and
  the gen-2 migration stays on `minds-v0.6.2`. Cut 0.6.3 only when something
  else needs a release; until then new desktop installs that have no cached
  base image cannot create local Docker workspaces on 0.6.2, and the
  imbue_cloud slow-path rebuild of 0.6.2 on a leased slice fails the same way.

- [ ] **SSH certificates replace the static management key on gen-2 boxes**
  (imbue-ai/mngr-internal#850; branch `mngr/ssh-authority-in-vault`). Order,
  per tier, dev first (dev and ci: steps 1 and 2 done 2026-09-09; the mounts are
  `minds-<tier>-ssh` and exist for every tier):
  1. `terraform apply` in imbue-ai/vault (branch `mngr/ssh-authority-in-vault`):
     the `minds-<tier>-ssh` mounts, CAs, roles, connector AppRoles, and the
     employee / tier-operator / `minds_ci_env_gh` sign grants.
  2. `vault read -field=public_key minds-<tier>-ssh/config/ca` and commit it as
     `[ssh_ca] public_key` in the tier's `deploy.toml` (the commented block is
     already there). The same PR must drop that tier from the pinned
     `test_committed_deploy_tomls_have_no_ssh_ca_until_the_tier_brings_one_up`
     in `apps/minds/imbue/minds/config/loader_test.py` (delete the test once
     every tier has a CA); it exists so the flip is deliberate. Mint the
     connector AppRole's role-id + secret-id into
     `secrets/minds/<tier>/ssh-ca` (`.minds/template/ssh-ca.sh`).
  3. `minds-admin env deploy`: pushes the `ssh-ca` Modal Secret and seeds the
     `ssh-management-certs` Dict by running the connector's `ssh_cert_refresh`
     once. On dev/ci a deploy before steps 1-2 still succeeds (the `ssh-ca`
     secret is pushed as a placeholder and the function logs that the
     AppRole is unset and stores nothing), but gen-2 boxes answer 503
     `management_certificate_unavailable` until it is populated. On
     staging/production the required-service check refuses the deploy
     until `secrets/minds/<tier>/ssh-ca` exists with both template keys
     (empty values are allowed), so create that entry before the tier's
     next deploy even if the CA itself comes later.
  4. **Destroy and repave the existing gen-2 dev canary boxes** (`server
     setup`, which reinstalls and preps with the CA trust). There is no
     migration for a gen-2 box prepped before this change: its VM root and
     containers still authorize the pool key and trust no CA, and the new
     tooling no longer holds a key they accept. That also means `pool destroy`
     from this version cannot SSH such a box: drop its rows with
     `--drop-row-only` (the reinstall discards the VMs anyway), and release
     leased rows through `minds-admin workspaces release`, which runs
     server-side. Done for the dev canaries on 2026-09-09.
  5. `just server-audit`: every gen-2 box must report `authorized_key_count
     = 0` and `is_trusted_ca_correct = true`; the box telemetry stream
     (`journalctl -t mngr-box-telemetry` on the box, or the tier's
     `box_logs` stream) must show `management_login` events and no
     `MANAGEMENT_SSH_ANOMALY` signal with reason `static_key_login`.
  Gen-1 boxes are untouched (they keep the pool key until the cutover's
  phase 6). Done for the CI tier on 2026-09-13: both standing CI boxes were
  repaved gen-2, and the warm-cache job and the teardown sweep run under the
  `ci-infra` activation so the runner's `minds_ci_env_gh` token signs their
  operator certificate (verified in the boxes' sshd logs). Staging: steps 1-3
  done on 2026-09-13 (CA committed in `3e97dd1ea2`, AppRole minted, deploy
  `20260913T160033Z` seeded the certificate Dict); step 5 waits on its first
  gen-2 box. Production: steps 2, 3 and 5 remain (needs the `minds_production`
  Vault role), plus its first gen-2 box.

- [ ] **Slice-fleet gen-2 connector migrations 034-041** (branches
  `new-fleet-base` -> `new-fleet-runsc-prototype` -> `new-fleet-phase-2` ->
  `new-fleet-phase-5.5`): the env deploy applies them in filename order.
  `039_sizing_not_null.sql` backfills `pool_hosts.memory_units` / `disk_gb`
  and makes both NOT NULL, and `040_uplink_mbps_not_null.sql` backfills
  `bare_metal_servers.uplink_mbps` to 1000 and makes it NOT NULL, so the
  tier's minds-admin (order / register / bake) and connector must be deployed
  from this version together -- an older admin checkout would insert
  sizing-less or uplink-less rows and fail. A gen-1 row's
  `disk_gb` is stamped with the size it has after the cutover (gen-1 disk +
  16 GiB). Dev envs that applied the pre-renumber filenames (`033`-`037`)
  need their `schema_migrations` rows inserted by hand before the deploy, or
  the deploy re-applies the renamed files:
  `INSERT INTO schema_migrations (version) VALUES ('034_slice_fleet_gen2.sql'), ('035_wg_public_key.sql'), ('036_machine_sizing.sql'), ('037_wireguard_column_names.sql'), ('038_pool_hosts_baking_rows.sql') ON CONFLICT (version) DO NOTHING;`
  `041_slice_column_names.sql` is additive (imbue-ai/mngr-internal#848): it
  adds `bare_metal_servers.slice_service_user` and
  `pool_hosts.slice_instance_name` / `slice_disk_name` and backfills them from
  the `lima_*` columns, which stay dual-written. A renamed checkout reads the
  new columns (`COALESCE(new, old)`), so it needs 041 applied before it can
  read a tier's pool DB -- including the standing CI infra DB, which
  `import-boxes` reads (apply 041 there by hand with `psql -f`). Done for
  the CI infra DB on 2026-09-10 (it was at 027; 028-041 applied with
  `apply_pool_hosts_migrations`, release dispatch 34430115740 then built
  the CI env). dev-josh-2 and the throwaway dev-gen2mig env (deployed
  from minds-v0.5.2, then redeployed from this branch) applied 034-041
  through `env deploy` in order. The dev tier's standing registry
  (`minds-dev-infra`, created 2026-09-16 with every migration through 043
  applied) is the dev counterpart of the CI infra DB and is read the same
  way by `import-boxes`, `env deploy`, and `wireguard sync-peers --tier dev`.

- [ ] **Connector migration 042 (`042_workspace_stop_kind.sql`)**: adds
  `pool_hosts.stop_kind` (why a workspace was stopped, and so who may start
  it again; `specs/workspace-stop-kinds.md`). Additive, no backfill (NULL
  reads as an owner stop); the env deploy applies it in filename order.
  `minds-admin cutover migrate` probes the connector for the stop-kind route
  before every stop and aborts against a connector without it, so each tier's
  connector must carry 042 before that tier's first migration. Done for
  staging on 2026-09-14 (deploy `20260914T135700Z`; see
  [history/minds-v0.6.0.md](./history/minds-v0.6.0.md)). Production gets it
  with its first phase-5.5 connector deploy (034-042 in one go).

- [ ] **Connector migration 043 (`043_wireguard_address_unique.sql`)**: a
  partial unique index on `bare_metal_servers.wireguard_address`, the
  schema-level guard behind the locked allocation `server prep` / `setup` now
  do (two concurrent production setups took the same overlay address on
  2026-09-15). It refuses to apply while a tier's pool DB still holds a
  duplicate, so before each tier's deploy run
  `SELECT wireguard_address, count(*) FROM bare_metal_servers WHERE
  wireguard_address IS NOT NULL GROUP BY 1 HAVING count(*) > 1;` and repair
  any hit (restamp one box to a free address, renumber its wg0, re-prep it;
  see the production repair in
  [history/minds-v0.6.1.md](./history/minds-v0.6.1.md)). Production was
  repaired the same day. Staging: checked (no duplicates) and applied by the
  0.6.2 deploy `20260917T054239Z` on 2026-09-17 (see
  [history/minds-v0.6.2.md](./history/minds-v0.6.2.md)). The dev envs are
  unchecked. Production: already at 043 when the 2026-09-20 deploy's preflight
  read its `schema_migrations` (applied by the 2026-09-17 production deploy
  `20260917T142813Z`, which no history entry records); the duplicate-address
  query was empty. Only the dev envs remain unchecked.

- [x] **Artifact mirror serving** (imbue-ai/mngr-internal#856, #851). Done
  2026-09-09 for production: `minds-admin artifacts upload` (14 artifacts),
  `apt-mirror cut` + `warm` + `verify` at `20260725T000000Z` (the docker
  archive had never been cut), and `just deploy-apt-mirror` (the Worker's
  `artifacts/` route only existed on the branch; the bucket check in
  `artifacts verify` passes while a stale Worker answers 400, so probe a
  public artifact URL after every upload until `verify` does it itself). A
  gen-2 prep downloads from the mirror with no upstream fallback, so all
  three must precede the first `server setup` / `prep` from this version on
  any tier.

- [x] **Re-prep the dev gen-2 canary boxes** once 041 is applied
  (`minds-admin server prep --server-id <id>`, with no stop/start in flight on
  the box): the prep converges each box onto the `slicehost` service user,
  removes `limahost`, stamps the row, and installs the slice DHCP server
  (imbue-ai/mngr-internal#849: `mngr-slice-dhcp.service` running dnsmasq as
  the unprivileged `mngr-dhcp` user with only `CAP_NET_BIND_SERVICE`, the
  tap-only udp/67 policy in `/etc/nftables.d/mngr-slice-dhcp.nft`, plus the
  helper's DHCP accept), which every slice carved or restored from this
  version on needs to get an address. Done on the hil dev canary
  (`03a9a4af`) on 2026-09-08, where the whole check passed: a bake, lease,
  adopt, stop, retention finalize and restore onto a different ordinal (0 to
  1) came back at the new /30 by DHCP with the adopted host key, root's
  `authorized_keys` and the single cloud-init instance all unchanged, and a
  VM reboot and a lease renewal re-leased cleanly; `just server-audit` was
  clean. Done for all three dev-josh-2 gen-2 boxes (`716159c2`, `d7c0d9a3`,
  `45dcd2a4`) on 2026-09-13 from the merged tree, which also installed the S3
  IPv4 pin and its telemetry-manifest entries; the audit stayed clean (4
  exclusive, 0 contaminated).

- [x] **Retire the pre-#849 dev gen-2 slices.** Slices carved before the DHCP
  change carry a static netplan for their original ordinal and cannot restore
  onto another ordinal or box (no replay applies the new address). Destroy and
  re-bake the dev boxes' `available` rows, re-create the leased dev
  workspaces, and `minds-admin workspaces release` their `stopped` rows. No
  staging or production gen-2 slice predates the change, so no compatibility
  path exists. Done: the leased dev-josh-2 workspaces were released on
  2026-09-12 and the last four `available` rows (baked from the deleted
  provisional tag) were destroyed on 2026-09-13; the dev pool is re-baked from
  the real `minds-v0.6.0` tag.

- [ ] **Deploy the production services.** Production runs connector
  `dabb19b95b`, whose `FALLBACK_BRANCH` is `minds-v0.4.3`, so browser creates
  (`/hosts/claim`) pin to that tag while desktop 0.5.0 clients ask for
  `minds-v0.5.0`. Until this deploys, keep `available` rows at `minds-v0.4.3`
  -- `/hosts/claim` matches the tag exactly and has no rebuild fallback.

  From `mngr/remote-workspace-fixes` on, the connector reads the web pin from
  the release feed's `<channel>-web.json` (`[web_channels.*]` in
  `apps/minds/release-channels.toml`, published by the channels workflow when
  that branch merges) and uses `FALLBACK_BRANCH` only while the feed cannot be
  read, so this coupling ends with that deploy. Before deploying: confirm
  every `curl -s https://updates.imbueminds.com/<channel>-web.json` (stable,
  beta, alpha) names a tag the production pool has `available` rows at -- the
  deployed connector leases exactly what each channel's file says (the entries
  land at `minds-v0.5.2`, matching desktop stable); repoint the web channels
  there if one does not. After deploying: exercise a web create from the chrome on each
  channel and check the connector log for `Could not read the web pin`
  (a feed read problem) or `No web pin published` (a channel file missing).
  Also check the three Modal web functions (`rsc-production` `api`,
  `llm-production` `proxy`, `oauth-redirector-production` `redirect`) run in
  a US region: `modal container list` (with `MODAL_PROFILE=minds-production
  MODAL_ENVIRONMENT=main`) names the live containers, and `modal container
  exec <id> -- sh -c 'echo $MODAL_REGION'` must print a `us-*` region (the
  Modal CLI has no `app describe`). Then confirm a lease/stop cycle against a
  gen-2 box still works through the proxy. Verified on staging on 2026-09-15
  (`us-west-2` for both the connector and the proxy after deploy
  `20260915T021647Z`). Production deployed on 2026-09-20 from `fb7c265c5f`:
  deploy `20260920T002726Z` (RECREATE; applied 044), `/version` advanced,
  both health checks green; all three `<channel>-web.json` files named
  `minds-v0.6.2`, at which the pool held 7 `available` US-EAST-VA and 17
  US-WEST-OR rows; the fresh connector log had no `Could not read the web
  pin` / `No web pin published` line; the four live `rsc-production` and
  `llm-production` containers answered `westus3` / `eastus2`. Not done: a web
  create from the chrome on each channel, and a lease/stop cycle through the
  proxy.

- [ ] **Promote 0.5.0 past alpha.** Beta and stable are still 0.4.2 (build
  `260825un55i8ix7`), so most users are two releases behind. The 0.5.0 build is
  `260902shwco3ynx`.

  Bump the connector download fallback (`_DEFAULT_TARGET_BY_PLATFORM` in
  `accounts_web.py`) in the same PR **only when the channel is `stable`** -- it
  is what the public download link serves while the feed is unreadable, and
  leaving it *ahead* of stable is unrecoverable, since `allowDowngrade` is false.

- [ ] **Bake the production pool at whatever tag is promoted**, before the
  `[web_channels.*]` repoint that pins browser creates to it
  ([ops/app-release.md](./ops/app-release.md) step 9b). The services deploy no
  longer pins to it: it reads the feed, so it goes first.

- [x] **Staging management-plane lockdown** before staging's first gen-2 box
  takes workspaces. Done 2026-09-13: the Modal proxy `mind-connector-east`
  (us-east, `98.90.51.49`), the `[management_plane]` table in
  `envs/staging/deploy.toml`, and the connector deploy `20260913T160033Z` that
  egresses from it (verified from inside the live container), and the first
  gen-2 box prep (`21ae4720`, repaved 17:35Z-17:49Z) installed the `:22`
  lockdown: public `:22` refused, allowlist `wg0` + the proxy IP, operator dial
  over onetun verified. Every further staging gen-2 prep locks down the same way.

- [ ] **Production management-plane lockdown**: the Modal proxy
  `mind-connector-east` (us-east, `52.206.40.121`) exists and the
  `[management_plane]` table in `envs/production/deploy.toml` is committed
  (2026-09-13); the connector deploy that attaches it and the first gen-2 box
  prep remain, after staging has rehearsed.

- [ ] **Pin the Modal apps to a US region.** Nothing passes `region=` to
  `@app.function`, and Modal schedules unpinned containers globally: the live
  dev connector was observed in `eu-central-2` and the freshly deployed staging
  connector in `spaincentral` (2026-09-13). Requests already route through
  Modal's default `us-east`, the proxies are in us-east, the boxes are in
  Virginia/Oregon and Neon in `us-west-2`, so every proxied SSH and every DB
  round trip currently crosses the Atlantic and back through the WireGuard
  tunnel. Thread `region="us"` (broad; ~1.15x) through `deploy.toml` for the
  connector, LiteLLM proxy and analytics apps, then redeploy each tier. The
  pin landed in `dcf4b7075c` on the web functions (crons stay unpinned);
  staging's deploy `20260915T021647Z` verified `us-west-2` for both the
  connector and the proxy containers. Production gets it with its next deploy.

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

## Gen-2 slice-fleet incremental migration (phase 5.5; see [gen2-cutover.md](./gen2-cutover.md))

- [x] From the release carrying the phase-5.5 stack on, gen-2 boxes bake only
  minds-v0.6.0+ tags and gen-1 boxes only older ones (the bake-time guard).
  The real `minds-v0.6.0` pair was cut on 2026-09-13 (mngr `5325e15e73`,
  dwt `96935db5b`; see [history/minds-v0.6.0.md](./history/minds-v0.6.0.md));
  the dev-josh-2 rows baked from the deleted provisional tags were destroyed
  and the dev pool re-baked from the real tag. `minds-v0.6.1` was cut and
  built on 2026-09-15 (mngr `0c9d81e7f6`, dwt `a87c68e19`, build
  `260915wjcyd06bp`; see [history/minds-v0.6.1.md](./history/minds-v0.6.1.md)),
  deployed to staging and baked on all three staging boxes (one 0.6.1 row
  each) on 2026-09-15; staging holds no gen-1 box or row any more.
  `minds-v0.6.2` was cut and built on 2026-09-17 (mngr `253e087a58`, dwt
  `073a56eb2d`, build `260917ohslvgoyj`; see
  [history/minds-v0.6.2.md](./history/minds-v0.6.2.md)), deployed to staging
  (`20260917T054239Z`) and baked on all three staging boxes (one 0.6.2 row
  each) the same night; it carries the fix for latchkey permission requests
  from a seeded welcome chat, which no earlier build has. Still to do: keep
  0.5.x stocked on production's gen-1 boxes until its create rate reads ~zero.
- [ ] **Production has no gen-2 box yet**, and the bake guard refuses
  `minds-v0.6.0` on gen-1 boxes, so it cannot hold 0.6.0 rows until its gen-2
  prerequisites land: the tier's `[ssh_ca]` committed (and dropped from the
  pinned `loader_test`), the connector AppRole in
  `secrets/minds/production/ssh-ca`, a connector deploy (the `[management_plane]`
  table is already committed), then a repaved or freshly ordered gen-2 box.
  Until then a 0.6.0 desktop there takes the slow path onto gen-1 rows and
  browser creates pinned to 0.6.0 find no row. Staging completed all of this on
  2026-09-13 (`21ae4720` repaved gen-2; see
  [history/minds-v0.6.0.md](./history/minds-v0.6.0.md)).
- [ ] **Draining a box costs the full stop-retention window before `cutover
  repave` accepts it** (a `stopped` row keeps its box link until the retention
  finalize). Staging and production now set the window to 600 s
  (`[storage] stop_retention_seconds`), which is the per-box floor for the
  production drain-and-repave sweep; supervisors already sleeping when the
  value changes keep their old window.
- [ ] Begin migrations per tier in the order dev -> staging -> production once
  the release carrying `minds-admin cutover migrate`/`rollback` and the
  connector's `max_box_generation` lease filter is deployed there (start with
  single alpha workspaces). Staging: the full stack (latchkey leg, stop kinds,
  the #970 supervisor fix) passed its drills on 2026-09-14 with both a real
  0.5.2 client and a branch client open (see
  [history/minds-v0.6.0.md](./history/minds-v0.6.0.md)). Staging is DONE as
  of 2026-09-15: every remaining workspace migrated, both gen-1 boxes repaved
  gen-2 (see [history/minds-v0.6.1.md](./history/minds-v0.6.1.md), whose
  findings list what the sweep taught the tooling: legacy-layout workspaces
  need `repair-home-layout` first, rows 039 could not measure are restamped,
  pre-0.5.0 templates need the tolerant seed bake). Production: the box
  sweeps started on 2026-09-20 (night 1), hil / US-WEST-OR only, two
  concurrent invocations at most (two lanes, one target box each), no
  repaves, every migrated workspace verified read-only on its target (DB row,
  state record, VM root over the operator certificate, container programs,
  home tree, version, UI). Per source box, in order:
  - **`946923eb` -> `95bbc37c`** (lane A, 06:32Z-10:23Z, 10/10): the first
    invocation carried `--publish-image-tars`, which seeded and published the
    0.4.1, 0.4.2 and 0.5.2 tars (about 6 minutes each) before the first
    harvest; 0.6.2 seeded at the first harvest and 0.5.0 came from lane B's
    seed. Rows, in migration order, with the new container port on
    `147.135.97.118` (the VM port is one below) and the latchkey detail:
    `96aefeda` imbue-hud (0.6.2, 22001, FULL), `c86f8f71` workspace-2
    (0.4.1, 22003, FULL), `7347c179` workspace-1 (0.4.1, 22005, none),
    `1cecae75` work (0.6.2, 22007, FULL), `e959cc6b`
    primary-shared-tools-workspace (0.6.2, 22009, FULL), `4f8da966`
    library-stuff (0.5.0, 22011, FULL), `d4c18c8d`
    shared-event-attendees-workspace (0.6.2, 22013, FULL), `d4f5cd82`
    workspace-1 (0.5.2, 22015, FULL), `9b8ab0dc` workspace-1 (0.4.2, 22017,
    none), `81c1728f` main (0.5.2, 22019, FULL). Stops took 1.5 to 3 minutes
    except `c86f8f71` (23 minutes: its data disk is 81% full). Two
    workspaces carry an owner-added supervisord program that is not running
    on gen-2: `c86f8f71`'s `sg-download` (`ionice -c3` is refused under
    gVisor: `ioprio_set failed: Operation not permitted`; the owner has to
    drop the `ionice` from its command) and `4f8da966`'s `disc-finder`
    (still STARTING at the probe). The box then held 0 rows; audit clean
    (`95bbc37c` 10/14 used, keys 0/0, CA correct, encrypted, 36 exclusive /
    0 contaminated). Not repaved.
  - **`0d59c281` -> `0172f93e`** (lane B, 07:36Z-10:53Z, 10/10; started once
    lane A's first workspace had passed its checks). Seeds paid on the
    target: 0.3.11, 0.5.0 and 0.6.1 (the 0.6.1 seed's first attempt died in
    the slice boot wait with "Connection to 127.0.0.1 closed by remote
    host" and the bake's own retry succeeded). Rows, in migration order,
    with the new container port on `51.81.243.47` and the latchkey detail:
    `20636a9c` workspace-2 (0.3.11, 22001, DISK_ONLY), `4d98c380`
    workspace-1 (0.4.2, 22003, none), `42a5cc82` workspace-2 (0.4.2, 22005,
    none), `c3241f4e` terrapin-trail (0.5.0, 22007, FULL), `cb868420`
    workspace-1 (0.4.2, 22009, none), `a341427b` workspace-5 (0.6.2, 22011,
    FULL), `cf6412be` workspace-1 (0.4.2, 22013, none), `42ebe272`
    workspace-2 (0.4.2, 22015, FULL), `e0a0fa65` workspace-1 (0.5.0, 22017,
    FULL), `9e1f7ee5` autocompact-null-test (0.6.1, 22019, FULL). Three of
    them (`4d98c380`, `42a5cc82`, `42ebe272`) were created from a branch
    and hold no release tag, so their version came from the vendored
    `FALLBACK_BRANCH` (0.4.2). The box then held 0 rows; the 10:25Z audit
    read `0172f93e` 10/14 used, keys 0/0, CA correct, encrypted. Not
    repaved.
  - `777fadf6` (the never-leased `available` 0.5.2 row on `9ef5ab2e`) was
    destroyed with `pool destroy` at 10:53Z, as agreed, before that box's
    sweep.
  - **`d6871163` -> `c2458e1c`** (lane A, 10:24Z-11:49Z, 7/7; no seed
    needed, every version's tar was in the bucket). Rows, in migration
    order, with the new container port on `51.81.242.229` and the latchkey
    detail: `26569bf0` workspace-3 (0.4.2, 22001, FULL), `d92d02b6`
    workspace-1 (0.4.2, 22003, FULL), `c9169345` clear-up-elm20-gmail
    (0.4.2, 22005, FULL), `14dfa404` google-inbox-aug27 (0.6.2, 22007,
    FULL), `62c66345` workspace-1 (0.4.2, 22009, FULL), `2ed5abf3`
    sept14-4pm (0.5.2, 22011, FULL), `190b5607` workspace-2 (0.6.1, 22013,
    FULL; its own `tailscaled` program was still STARTING at the probe).
    The box then held 0 rows. Not repaved.
  - **`9ef5ab2e` -> `d0aae071`** (lane B, 10:54Z-12:34Z, 7/7; no seed
    needed). Rows, in migration order, with the new container port on
    `51.81.243.34` and the latchkey detail: `bc48bb25` workspace-1 (0.4.1,
    22001, FULL), `b8dc7438` work (0.6.2, 22003, FULL; its own
    `relationships` program was still STARTING at the probe), `6f53fb3f`
    personal (0.6.2, 22005, FULL), `8b594773` workspace-1 (0.4.1, 22007,
    none), `cfa65b4a` shared-analytics-workspace (0.6.2, 22009, FULL),
    `68ebc224` gmail-ui (0.4.2, 22011, FULL), `f210f8b6` daisyui-skill
    (0.5.2, 22013, FULL). The box then held 0 rows. Not repaved.
  - **`7286e152` -> `c2458e1c`** (lane A, 11:49Z-13:04Z, 6/6; no seed
    needed; the target then held 13/14). Rows, in migration order, with the
    new container port on `51.81.242.229` and the latchkey detail:
    `7c6b3c64` workspace-1 (0.3.11, 22015, DISK_ONLY; the second 0.3.11
    migration, clean with the autostart fix), `76d51c10` workspace-5 (0.6.2,
    22017, FULL), `3e8c9573` kanjun-inbox-aug26-2pm (0.4.2, 22019, FULL),
    `300b9082` workspace-1 (0.4.2, 22021, none), `94dbf60d` workspace-1
    (0.4.2, 22023, none), `23f286bb` workspace-1 (0.4.2, 22025, FULL). The
    box then held 0 rows. Not repaved.
  - **`267e76bd` -> `d0aae071`** (lane B, 12:34Z-13:51Z, 5/5; no seed
    needed; the target then held 12/14). Rows, in migration order, with the
    new container port on `51.81.243.34` and the latchkey detail:
    `245ea378` workspace-1 (0.3.11, 22015, FULL; the third and last 0.3.11
    migration, clean), `093dff1b` financial-workspace (0.6.2, 22017, FULL),
    `6dba5432` workspace-6 (0.6.2, 22019, FULL), `c7753264` workspace-1
    (0.5.0, 22021, FULL), `fe8d7a08` workspace-1 (0.6.2, 22023, FULL; its
    own `task-inbox` and `task-inbox-refresh` programs read STOPPED "Not
    started", i.e. not set to autostart, as before the move). The box then
    held 0 rows. Not repaved.
  - **`782ed40b` -> `3841df6d`** (wave 3, 13:04Z-13:53Z, 3/3; no seed
    needed; the target then held 7/14 with its four earlier rows). Rows,
    in migration order, with the new container port on `147.135.97.227` and
    the latchkey detail: `5ecc0d7a` workspace-1 (0.6.2, 22005, FULL),
    `f482e124` workspace-6 (0.6.2, 22011, FULL), `6e80e84f` workspace-1
    (0.5.2, 22013, FULL). The box then held 0 rows. Not repaved.
  - **Night-1 end state (13:55Z)**: 48/48 workspaces migrated and verified
    (every one `leased`, `box_generation` 2, `US-WEST-OR`, `stop_kind`
    NULL); the seven gen-1 source boxes `946923eb`, `0d59c281`, `d6871163`,
    `7286e152`, `9ef5ab2e`, `267e76bd`, `782ed40b` hold 0 rows and 0 slice
    disks (audit), left `ready` on gen-1 as the rollback horizon, NOT
    repaved; final `server-audit` 36 boxes, 36 exclusive, 0 contaminated,
    0 unaudited, every gen-2 target keys 0/0, CA correct, encrypted, no
    degraded arrays. No rollback was run and no cutover record is in
    flight; every harvested key was shredded. Reports:
    `~/.minds/cutover/reports/migrate-20260920T*.json`. Timings: 10 to 18
    minutes per workspace when its tar was in the bucket, about 6 minutes
    per seed; the whole 48 took 06:32Z to 13:53Z with two lanes. Owner
    follow-ups: `c86f8f71`'s `sg-download` cannot run under gVisor (drop
    `ionice -c3`); the other owner-added programs the probe reported were
    STARTING or deliberately not autostarted. Still to do for production:
    the remaining gen-1 boxes (`46bf5609` with its do-not-touch row,
    susy's three boxes, every vin box), the repaves once the rollback
    horizon closes, and the retired rows' releases.
  - Two tooling gaps found and fixed on this branch during the sweep, each
    on the first workspace it applied to: (1) the `minds-v0.3.11` template's
    autostart installer only enables its boot unit, so a 0.3.11 workspace
    came back with no services agent and failed the health probe (row
    parked about 20 minutes, then resumed with the fix: the replay now
    starts the unit); (2) the health probe required every supervisord
    program, so `c86f8f71`'s own gVisor-incompatible program failed the
    migration (row parked about 30 minutes, then resumed with the fix: only
    template-shipped programs block; owner-added ones are reported). Both
    resumes were `--workspace <row>` plus the box sweep in one invocation,
    so the parked row went first.
  - **Daytime 2026-09-20 (14:00Z to 18:30Z), between the night-1 and night-2
    sweeps**: night 1 validated clean (Bugsink `rsc` 0 issues first seen after
    06:00Z; 0 `MNGR_BOX_SIGNAL` and 0 OOM lines on the five targets; the
    migrate's own footprint in OpenObserve was 47 admin stop-kind 409 probes,
    one owner start 409 during a hold, three slice-reconcile divergences
    mid-transplant, and seven harmless gen-1 retention finalizes). The
    tier-wide `repair-home-layout --all-leased` probe found six legacy
    layouts among leased rows, all repaired with `--migrate` (~1 minute each,
    rollback snapshot kept): `ff6927e5` red-scribble and `dd887893`
    workspace-1 on `0b24ee94`, `20dfb490` workspace-2 on `219affc1`,
    `3584ef5c` workspace-1 on `feb11eae`, `7f574268` a and `1267becb` b on
    `a7828ee9` (vin). The tier preflight (0 refused, 10 errors) named the
    six, the three do-not-touch rows, and `8499e566` on `a7828ee9`
    ("expected exactly one container labeled with host id ..."); health
    warnings on `ffefcef6` (supervisord down) and three owner-added programs.
    Three accounts suspended in late August as abusers (prefixes
    `77d44ec6ad0f431d`, `f3bfb0c10dbd4695`, `a69c29083b964fba`) had their
    stopped gen-1 rows (`187a39a4`, `bf3bb7d2`, `ff300196`) released and the
    accounts deleted with `scripts/delete_accounts.py`; their `host-...`
    buckets were left to the backup-retention reaper. A trial migrate of
    `ff300196` before that deletion admin-started it onto `7286e152`, was
    refused at the harvest for a legacy layout, and the repair then failed
    (no services in the restored container; the owner's `.bashrc` runs an
    endless restic loop); stopped again with `workspaces stop --kind idle`
    and released (the migrate does not check owner suspension: issue 1164).
    Four departed internal accounts (prefixes `3e9e8a959bb74a3c`,
    `91beeb7f32554fb3`, `4bf48af6fa144631`, `96803d7d156f484f`) were
    removed: five rows released (`c3fc9ee3` leased on `80972293`,
    `9ea236f0` stopped, and the retired `80c7dd0c`, `7566937b`, `7bbbb887`,
    whose archives survive), accounts deleted, and their ten R2 buckets
    emptied and deleted through the connector's own bounded reaper (7,665
    objects, ~66 minutes) with the ten Cloudflare tokens revoked. Then the
    stopped-row path was proven: the two stopped vin workspaces of one
    internal owner (`f00fd0f9` workspace-6 and `d8f23a08` workspace-7, both
    0.3.11) migrated vin to vin onto `8797d3ae` (17:59Z to 18:19Z, ~10
    minutes each): admin start onto a vin gen-1 box (`d157ec61`, then
    `8cade5fa`), harvest (latchkey `DISK_ONLY`, as a stopped origin has no
    tmpfs pair), `disk_gb` restamped 44 to 45 from the measured 29 GiB disk,
    then the usual stop, park, transplant, replay, re-lease; new container
    ports 22011 and 22013 on `135.148.169.102`, both verified (`leased`, gen
    2, `US-EAST-VA`, runsc, all template programs, UI 200); the `:1989`
    gateway stays unbound until the owner's desktop next provisions the
    machine. Pre-existing problems found on the way were filed as issues
    1157 to 1163. Reports `~/.minds/cutover/reports/migrate-20260920T181914Z.json`.
  - **Evening 2026-09-20 (21:15Z to 22:15Z)**: the last stopped hil gen-1
    row of a non-internal owner, `f21a2f5c` workspace-1 (owner prefix
    `9f13747fa3434371`, 0.4.1, stopped since 2026-08-24, `disk_gb` 44),
    migrated onto `3841df6d` (21:19Z to 21:32Z). The connector admin-started
    it onto `feb11eae` (the same-region gen-1 box with room it picked; the
    box was back to its 8 rows afterwards), harvest `DISK_ONLY`, `disk_gb`
    restamped 44 to 45, tar already in the bucket; new container port 22015
    on `147.135.97.227`, verified (`leased`, gen 2, `US-WEST-OR`, runsc,
    `/home/user -> /mngr-vol/home`, 1.2 GB, `minds-v0.4.1-4-g...`, UI 200,
    `mngr list` shows `system-services`); `3841df6d` then held 8/14. Report
    `migrate-20260920T213245Z.json`. Then **the seven emptied hil gen-1
    boxes were repaved gen-2** with `cutover repave`, two concurrent batches:
    `946923eb`, `0d59c281`, `d6871163`, `7286e152` (21:37Z to 21:54Z) and
    `9ef5ab2e`, `267e76bd`, `782ed40b` (21:56Z to 22:11Z), every box coming
    back `ready`, generation 2, 14 slots, overcommit 4.0, LUKS-encrypted,
    keys 0/0, CA correct, storage partition ~936.5 GB (reports
    `repave-20260920T215348Z.json`, `repave-20260920T221116Z.json`; per-box
    records in `~/.minds/cutover/boxes/`). They are migration targets, not
    stock (no bake). This closes the rollback horizon for the 48 night-1
    workspaces as far as those boxes go; the remaining hil gen-1 boxes
    (`219affc1`, `529bf614`, `80972293`, `91d1f669`, `46bf5609`, and the
    three do-not-touch boxes) and vin's still exist, and the horizon has not
    been declared closed. The quarantined never-leased 0.6.2 row `07190358`
    on `5f099257` (ordinal 10; issue 1161) was destroyed with `pool destroy`
    at 21:37Z, so that box reads 12/14 again (no replacement baked). Two
    read-only diagnoses for the night sweeps, remedies pending: `ffefcef6`
    workspace-1 on `80972293` has been running with no services since its
    owner's stop/start on 2026-09-17 05:20Z, because an `update-self` run
    earlier that night (00:30Z to 02:00Z, merged 0.6.1 then rolled back the
    apply, never reaching a verdict) left the container with no `mngr` uv
    tool, so the template's `minds-autostart.service` fails with
    `exec: mngr: not found` and, with `StartLimitIntervalSec=0` and the path
    unit retriggering it, hot-loops about six times a second (over a million
    failures since 09-19); the fix is to reinstall the vendored mngr
    (`system/scripts/install_mngr.py`) in the container, after which the
    autostart brings the services back. `8499e566` workspace-1 on `a7828ee9`
    (vin) is a failed slow-path create from 2026-09-11 18:08Z (lease, volume
    wiped to an empty `agents/` + `host_dir/`, the rebuild's `docker build`
    canceled at 18:29Z): no container at all, empty data volume, no backup
    bucket, a stub workspace record; the owner (client 0.5.0, last seen
    2026-09-17) fell back to a fast-path lease of `947649dd` workspace-2 on
    `c011f511` nine minutes later, which is healthy (all template programs,
    UI 200, home layout) and was mis-attributed the "no running container"
    probe verdict in the handoff. Both remedies were then applied with Josh's
    approval (22:25Z to 22:35Z): the vendored mngr was reinstalled in
    `ffefcef6`'s container with the installer's single `uv tool install`
    (`system/vendor/mngr/libs/mngr` plus the six `mngr_plugins.toml` plugins,
    `--reinstall`, tool dirs pinned to `/root`), after which the looping
    autostart started the services agent on its next tick and every template
    program, the UI and `mngr list` came back; the box's preflight then read
    clean. `8499e566` was released with `minds-admin workspaces release`
    (its stub record was deleted outright; `a7828ee9` then held 7 rows).
  - **The 22 stopped gen-1 workspaces of one internal owner (prefix
    `f01706cbe567466f`; 17 hil, 5 vin; baked 0.3.10 to 0.5.2) archived and
    retired at the owner's request (22:42Z to 00:19Z)**, the retire flow run
    on stopped rows: per row `archives create --start-stopped` (the connector
    admin-started each onto a same-region gen-1 box of its choosing, which
    included `feb11eae` and `46bf5609` as transient slots), a check with
    `archives list`, then `workspaces retire`; two lanes, hil and vin, about
    3.5 minutes per row. Every row has exactly one archive (46.5 GB in
    total); `archives links` wrote
    `~/.minds/archives/reports/archive-links-20260921T002003Z.{md,csv}`
    (7-day links) for the owner. Two rows failed once and succeeded on a
    re-run: `39afd238` and `7275de1c` hit an archive-streamer gap (a pytest
    fixture file under the container's `/tmp` with a synthetic year-2286
    mtime overflowed the zip's DOS date field; fixed on this branch, see
    `apps/minds_admin`'s changelog), and `690f5346`'s first admin start
    failed in the restore transfer on `91d1f669` (`zstd` decompress of the
    6 GB disk object; the row landed back on `stopped` and the retry
    restored cleanly). None released yet: release them once the owner has
    what he wants.
  - **Vin sweep, 2026-09-21 01:05Z to 02:29Z: the eight leased rows of
    `c011f511`, `d157ec61` and `642c2c1c` migrated vin to vin**, selected
    with `--workspace` (not box sweeps) so the 19 never-leased 0.5.2
    `available` rows on `c011f511` and `642c2c1c` were left as stock. Two
    lanes, one target each; every workspace verified read-only on its
    target. Lane A onto `f763270e` (empty; its first 0.3.10 workspace paid
    a seed), rows in migration order with the new container port on
    `135.148.169.103` and the latchkey detail: `2ff3ee72` mshq (0.3.11,
    22001, FULL), `e22f064d` workspace-1 (0.3.11, 22003, FULL), `947649dd`
    workspace-2 (0.5.2, 22005, FULL), `34cd5427` workspace-1 (0.3.10, 22007,
    FULL), `ffe92996` glebs-corner (0.5.2, 22009, FULL; its own `todo`
    program was still STARTING at the probe). Lane B onto `8797d3ae`, on
    `135.148.169.102`: `ec31ceb0` workspace-1 (0.4.2, 22015, none),
    `4546d8fd` workspace-1 (0.4.2, 22017, FULL), `4f2c7622` workspace-1
    (0.4.2, 22019, FULL; its checkout is a `_darcs` tree with no `.git`, so
    the version came from the vendored mngr's `FALLBACK_BRANCH`). `d157ec61`
    then held 0 rows (repave-eligible, like `8cade5fa`); `f763270e` 5/14,
    `8797d3ae` 10/14 (6 leased); audit 35 exclusive, 0 contaminated. One
    failure, fixed in place and resumed: `ffe92996` failed the migrate's
    autostart start because its `mngr` uv tool environment (under
    `/home/user/.local`, carried on the volume) had never been re-synced
    after the workspace's own update from 0.3.10 to 0.5.2, so the vendored
    mngr's import of `watchdog` failed on the first restart in months;
    the tool was reinstalled in the container on the target slice (the same
    remedy as `ffefcef6` earlier that night), the services came back, and a
    re-run of the same invocation resumed the parked row to RESTORED within
    two minutes. Reports `migrate-20260921T014633Z.json`,
    `migrate-20260921T015559Z.json`, `migrate-20260921T022335Z.json`,
    `migrate-20260921T022904Z.json`. Remaining gen-1 leased rows after this:
    hil `219affc1` 11, `529bf614` 11, `80972293` 10, `91d1f669` 12,
    `46bf5609` 7, `0b24ee94` 9, `feb11eae` 8, `bab2c8a1` 4; vin `a7828ee9` 6,
    `68069cdb` 7, `d04e8224` 10.
  - **02:37Z to 03:04Z: vin repaves and the last three retirements.**
    `d157ec61` and `8cade5fa` (both empty) repaved gen-2 (02:37Z to 02:53Z,
    `ready`, 14 slots, encrypted, keys 0/0, CA correct). With Josh's blanket
    approval of never-leased `available` rows as disposable capacity, the 19
    such 0.5.2 rows on `c011f511` (11) and `642c2c1c` (8) were destroyed
    with `pool destroy` (19/19) and both boxes repaved: `642c2c1c` came up
    `ready` gen-2 (02:47Z to 03:00Z); **`c011f511` failed its prep at the
    storage-encryption step, twice, with the TPM refusing to create the
    sealing key** (`Esys_CreatePrimary` error `0x9a2`, "Failed to seal to
    TPM2: State not recoverable"; the reinstall itself succeeded, the
    recovery passphrase is in Vault). The box sits at `installing`,
    generation 2, with no rows and is excluded from every pool path until
    its TPM is reset or the box is retired; the audit reports it unaudited
    meanwhile. Diagnosis (03:35Z): the TPM is detected and answers, but all
    three hierarchies carry unknown authorization values (`ownerAuthSet`,
    `endorsementAuthSet`, `lockoutAuthSet` all 1; `tpm2_createprimary`
    fails with 0x9A2 "authorization failure" under every hierarchy), and
    with the lockout auth set it cannot be cleared from the OS. OVH ticket
    737351 filed 03:38Z asking for a BIOS-level clear or a reseat/replace,
    referencing ticket 737104 (the `ns1002940` TPM that a reseat fixed on
    09-19). The three remaining pre-floor gen-1
    workspaces, live-probed RETIRE on 09-20 and left alone until now, were
    archived and retired at Josh's instruction (02:37Z to 02:41Z):
    `6d2e93e9` susyworkspace (owner prefix `2aebc26bbd8a4e1d`, 819.6 MB),
    `b6564921` terrapintrail2 (`1e5f65b5da5b40d0`, 864.8 MB), `6d238b19`
    workspace-1 (`c196c542cf0d4348`, 196.5 MB); links report
    `~/.minds/archives/reports/archive-links-20260921T025320Z.{md,csv}`.
    No gen-1 row below the version floor remains in any status but
    `retired`; 74 retired rows await release.
  - **Vin sweep, 2026-09-21 06:21Z to 08:29Z: `d04e8224` and `68069cdb`
    emptied, plus the one default-size row of `a7828ee9`** (Josh's vin
    plan; the hil sweep ran concurrently from another session on its own
    branch). Step 0 read clean: the 23 leased rows all on the home layout,
    every preflight CLEAN, no suspended owners, no overlay duplicates. The 4
    never-leased 0.5.2 `available` rows of `d04e8224` and the 7 of `68069cdb`
    were destroyed first (11/11). Two lanes, every workspace verified
    read-only on its target. Lane A **`d04e8224` -> `8cade5fa`** (06:21Z to
    08:29Z, 10/10), rows in migration order with the new container port on
    `51.81.56.168` and the latchkey detail: `d8017b2e` workspace-1 (0.3.11,
    22001, FULL; 14 GB home), `7e9cd93e` workspace-2 (0.3.11, 22003, none;
    23 GB), `1c1a894c` workspace-1 (0.3.11, 22005, FULL), `5d3f5971`
    workspace-2 (0.3.11, 22007, FULL), `b40cbe37` workspace-3 (0.3.11,
    22009, FULL), `2b6bff65` workspace-4 (0.3.11, 22011, FULL), `abc403b7`
    workspace-5 (0.3.11, 22013, FULL), `9afd33f1` workspace-6 (0.3.11,
    22015, FULL), `296e8f9c` workspace-1 (live 0.6.2 from a 0.4.2 bake,
    22017, FULL), `eba7ce5a` workspace-1 (0.4.2, 22019, FULL). Lane B
    **`68069cdb` -> `d157ec61`** (06:31Z to 08:09Z, 7/7; the 0.4.4 tar was
    seeded on the target), on `135.148.122.19`: `297ef0bb` talent-filter
    (0.3.11, 22001, FULL), `716aded5` workspace-2 (live 0.6.2, 22003, FULL),
    `a0933323` workspace-2 (0.4.1, 22005, FULL), `27b5cdb8` workspace-1
    (0.4.1, 22007, none), `a43a63bc` workspace-1 (0.4.1, 22009, FULL),
    `1b0da834` datalib-slack3 (live 0.4.4, 22011, FULL), `52525ddb`
    workspace-5 (live 0.6.2, 22013, FULL). Then `40a5634e` workspace-2
    (0.3.17, the one default-size row of `a7828ee9`, `disk_gb` restamped 44
    to 45 from its measured 29 GiB disk) -> **`642c2c1c`** at
    `135.148.34.21`:22001 (08:11Z to 08:21Z, DISK_ONLY). One failure, fixed
    in the tooling and resumed: `296e8f9c` failed the autostart start after
    the replay, because its checkout is a rolled-back self-update (content
    from before the 0.6.2 merge, `git describe` still 0.6.2) whose vendored
    mngr lacks the `mngr_autocompact` plugin the 0.6.2 image's tool
    environment registers (`ModuleNotFoundError: No module named
    'imbue.mngr_autocompact.plugin'`); the by-hand reinstall that fixed
    `ffefcef6` and `ffe92996` was discarded by the resume (each replay
    recreates the container from the image), so the migrate now runs the
    template's own `install_mngr.py` procedure inside the container and
    retries the start once (`0d2ce87030`; see `apps/minds_admin`'s
    changelog); the second resume then restored the row in 8 minutes.
    `68069cdb` (0 rows) was repaved gen-2 08:11Z to 08:24Z (`ready`, 14
    slots, encrypted, storage partition 936.5 GB); `d04e8224`'s repave
    started 08:30Z. Targets afterwards: `8cade5fa` 10/14, `d157ec61` 7/14,
    `642c2c1c` 1/14. Reports `migrate-20260921T074539Z.json`,
    `migrate-20260921T080832Z.json`, `migrate-20260921T082917Z.json` (lane A
    and its two resumes), `migrate-20260921T080929Z.json` (lane B),
    `migrate-20260921T082146Z.json` (`40a5634e`), `repave-20260921T082445Z.json`.
  - **08:30Z to 10:13Z: the five oversized-disk rows of `a7828ee9` shrunk
    onto `642c2c1c`, the first live use of `--shrink-oversized-disks`.**
    Each row carried `disk_gb` 232 (a 216 GiB gen-1 data disk holding 2 to
    6 GiB); the migrate measured the disk at the harvest, restamped the row
    232 -> 44 before the stop, and transplanted into a 44 GiB gen-2 disk.
    One proving run first (`3b1a5251` test-ui, live 0.6.2, 08:30Z to
    08:47Z, container port 22003, FULL), verified with `disk_gb` 44 in the
    row, a 44G data disk on the VM (12G used: the engines' base plus the
    2.5 GB home tree), UI 200 and the gateway bound; then the other four in
    one invocation, on `135.148.34.21`: `0456abbc` workspace-1 (0.5.0,
    22005, FULL), `a6a08154` workspace-1 (0.5.0, 22007, none), `7f574268` a
    (0.5.0, 22009, FULL), `1267becb` b (0.5.0, 22011, FULL). The
    216 GiB disks made each stop about 15 minutes. One failure, fixed in the
    tooling and resumed: `7f574268`'s replayed latchkey gateway went FATAL
    because its owner's desktop (a build from the unmerged
    `mngr/new-desktop-egress` branch) had written a `gateway_run.sh` naming
    `/usr/local/bin/latchkey-curl-router`, while this checkout's latchkey
    provisioning installs `latchkey-curl-dispatch`; the replay now reads the
    path off the harvested script and symlinks it onto the installed shim
    (`8720404f97`; the desktop's next provisioning pass replaces the link),
    and the resume restored the row in 8 minutes, with `1267becb` (the same
    owner) going through the new path directly. `f5b2043e`, the never-leased
    232 GB `available` row, was destroyed at 10:14Z and **`a7828ee9` was
    repaved gen-2** (10:14Z to 10:43Z: `ready`, 29 slots, overcommit 4.0,
    encrypted, storage partition 7.98 TB). `642c2c1c` then held 6 rows (one
    at 45 GiB, five at 44 GiB). Reports `migrate-20260921T084745Z.json`,
    `migrate-20260921T094517Z.json`, `migrate-20260921T101316Z.json`,
    `repave-20260921T104242Z.json`. **Vin end state (10:45Z)**: 23/23
    workspaces migrated and verified; no gen-1 box with rows is left in vin
    (`c011f511` stays `installing` on its TPM fault, OVH ticket 737351); the
    three repaved boxes are migration targets, not stock (no bake); none of
    the vin migrations left a cutover record in flight and every harvested
    key was shredded (the hil lanes were mid-flight at the time); final
    audit 34 exclusive, 0 contaminated, 1 unaudited (`c011f511`).
- [ ] `CLEANUP: drop the lima_service_user / lima_instance_name /
  lima_disk_name columns (a follow-up connector migration) and the dual writes
  and COALESCE reads marked CLEANUP in minds_admin and the connector once
  every tier's pool DB has applied 041 and no pre-rename checkout is in use`.
- [ ] **Connector migration 044 (`044_workspace_stop_kind_retired.sql`)**:
  widens the `stop_kind` check constraint with `retired`, the final kind
  `minds-admin workspaces retire` stamps on the workspaces the migration
  cannot take (after `minds-admin archives create` archived them; runbook
  section "Retiring the workspaces the migrate cannot take" in
  [gen2-cutover.md](./gen2-cutover.md)). The desktop and the plugin in this
  release render it; an older desktop shows a retired workspace as a plain
  hold ("not actionable") and its start subprocess is refused by the
  connector either way. Done for staging on 2026-09-19: deploy
  `20260919T235027Z` from `0325c2b42f` (`main` at the merge of PR #1145)
  applied 044 (RECREATE; staging was at 043, no duplicate overlay addresses)
  and shipped the archive/retire admin commands' connector side. The full
  archive -> check -> retire -> links flow was then exercised end to end on a
  fresh 0.6.2 staging workspace (row `708b76a2`, gen-2, runsc), which found
  and fixed `archives create` refusing every runsc container (it read the
  writable layer off the sandbox pid's mount table; it now falls back to the
  VM's rootfs mount). Done for production on 2026-09-20: deploy
  `20260920T002726Z` from `fb7c265c5f` (`main` at the merge of PR #1101 plus
  the runsc archive fix, whose connector code equals `main`) applied 044
  (RECREATE; production was at 043, no duplicate overlay addresses). The
  first production retirements followed the same night: the 54 gen-1
  workspaces in Josh's `~/handoff/ready-to-retire.txt` (baked 0.3.1-0.3.9,
  every live probe `RETIRE` with an empty `git describe`, every archive
  present in the bucket with a sha; 54.0 GB archived) were retired one call
  each between 01:5xZ and 01:58Z and had all reached `stopped` / `retired` by
  02:07Z; `archives links` wrote
  `~/.minds/archives/reports/archive-links-20260920T020713Z.{md,csv}`. Three
  more rows read `RETIRE` in that probe but were not on the list and were
  left alone (`b6564921` terrapintrail2, `6d238b19` workspace-1, `6d2e93e9`
  susyworkspace). A 55th, `326e3e1e` learn-from-scratch (nayana, baked
  0.3.10, `stopped` since 2026-08-18), was retired WITHOUT an archive on
  Josh's instruction: its stop artifact is encrypted to a lost key and the
  owner's restic bucket `c04e09c5f863425f--host-bae87c9771e14fe09ab08ca2e93aae3e`
  is the remaining copy. None has been released yet.
- [ ] `CLEANUP: delete s3://<bucket>/<prefix>archives/ (the retired
  workspaces' owner archives) 90 days after the last `minds-admin workspaces
  release` of a retired row` -- the archives are outside both the
  `<host_id>/` prefixes the release deletes and the `cutover/` prefix below,
  so nothing else reclaims them.
- [ ] `CLEANUP: delete s3://<bucket>/<prefix>cutover/ (the rollback copies of
  migrated workspaces' stop artifacts plus `images/<tag>.tar.zst`) after
  <date well past the last migration>`. Then delete the `cutover` command
  group and the connector's parked-row guard `_raise_if_workspace_is_migrating`
  (phase 6; requires zero gen-1 rows in every tier, all statuses).

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
