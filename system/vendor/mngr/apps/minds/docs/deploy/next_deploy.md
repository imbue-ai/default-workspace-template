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
  through `env deploy` in order.

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
  `20260915T021647Z`).

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
  each) on 2026-09-15; staging holds no gen-1 box or row any more. Still to
  do: cut further 0.6.x releases for the gen-2 cohort and keep 0.5.x stocked
  on production's gen-1 boxes until its create rate reads ~zero.
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
  pre-0.5.0 templates need the tolerant seed bake). Production remains.
- [ ] `CLEANUP: drop the lima_service_user / lima_instance_name /
  lima_disk_name columns (a follow-up connector migration) and the dual writes
  and COALESCE reads marked CLEANUP in minds_admin and the connector once
  every tier's pool DB has applied 041 and no pre-rename checkout is in use`.
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
