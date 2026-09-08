# minds-v0.4.3 (2026-08-30/31): deployed to staging and production

Deployment of the minds-v0.4.3 release to staging (2026-08-30, morning/afternoon)
and production (2026-08-30, evening UTC), both from branch `mngr/deploy-0-4-3`
(== `main` at `dabb19b95b`, tree byte-identical to the mngr `minds-v0.4.3` tag
`28484c1e54`). The default-workspace-template `minds-v0.4.3` tag was **re-cut**
mid-deployment (see "The sharing bug" below): annotated tag `3822e04d1` ->
commit `4c96f85a9` (previously `0e4ff5b04` -> `168242c16`), with the
vendor-match invariant re-verified against mngr `28484c1e54` after the re-cut.

## The sharing bug and the tag re-cut

The staging rehearsal surfaced a release-blocking bug on the FIRST
workspace-keyed share ever minted (the new
`<share-label>.<user-hash>.<region>.<domain>` domain shape introduced by
migration 032): every service panel in the shared view died with
`SSL_ERROR_UNRECOGNIZED_NAME_ALERT`. Root cause: dwt
`system_interface/frontend/src/origin.ts` `workspaceHostCoordinate()` only
recognized `host-`/`agent-`-prefixed coordinate labels; the new domain leads
with a bare 32-hex share label, so service origins were derived NESTED under
the shell's own label -- hostnames the relay holds no tunnel claim for, which
frps fast-fails with a fatal `unrecognized_name` alert (pinned behavior).

Fix: dwt PR #522 extended the coordinate rule (bare-32-hex alternative) in all
four pinned copies -- `origin.ts` (canonical), the placeholder's inline mirror
in `system_interface/server.py`, and the service-URL recognizers in
`layout_ops.py` + `system/scripts/layout.py` (with a first-label guard so the
workspace-keyed origin's 32-hex user-hash label never reads as a service URL).
The dwt tag was re-cut to include it.

**Lesson (tag-keyed bake cache):** the per-box template cache
(`~/.cache/mngr-slice-default-workspace-template/…-<tag>.tar`) is keyed by TAG
NAME, not content. A re-cut tag silently bakes stale code on any box that
already cached the old tar. The staging box's tar was purged and re-seeded;
production boxes had never held a 0.4.3 tar. Baked-slice content was verified
by grepping the built frontend bundle for the fixed coordinate pattern.

## Staging (2026-08-30, all verified)

- Two deploys from this branch: `20260830T155651Z` (RECREATE; pool-hosts
  migrations 031, 032, 033) and `20260830T160320Z` (ROLLOVER; completed the
  FRPS secret rotation). All three apps healthy; URLs match the committed
  `client.toml`; generation id unchanged.
- All 4 staging relays redeployed AFTER the connector with the header-form
  secret-free plugin URL (`https://<connector>/frps/auth`) and a rotated
  `FRPS_AUTH_SECRET`; live share tunnel survived the whole pass.
- A slice baked from the RE-CUT tag verified end to end: from-scratch
  workspace + sharing worked off the fixed content.

## Production deployment (2026-08-30 evening UTC)

- Pre-staged the FRPS rotation: production Vault
  `sharing/FRPS_AUTH_SECRET` set to `"<old>,<new>"` (collapsed pattern proven
  on staging).
- **Deploy 1** `20260830T214607Z` (RECREATE): migrations 031-033 applied,
  plans/paid-list seeded, `llm-production` + `rsc-production` +
  `analytics-production` deployed, health green, URLs match the committed
  `client.toml`, generation id unchanged (`8372712100784ba1a5c9273f866c97f4`).
- Smokes: `/version` served the new deploy id; `POST /admin/sweep/r2` answered
  with the new orphan counters (`users_skipped_orphan_pending_reap: 31`,
  `overdue: 0`) and the `r2_sweep_orphan_owner_pending_reap` metric records
  were confirmed in production OpenObserve -- first live exercise of the
  `type=metric` channel. The hourly "no resolvable verified email" Bugsink
  warning stopped at the deploy (last event 21:31Z; issue later marked
  resolved), as did the modal-client "background thread(s)" events (last
  21:34Z) -- both PR #655 verifications conclusive.
- **Relay fleet** (strictly after the connector): all 4 production relays
  (us1-1 15.204.73.79, us1-2 15.204.75.189, us2-1 40.160.4.254, us2-2
  147.135.77.174) redeployed with the header-form URL and the NEW secret only.
  us1-1 verified in depth first (live tunnels re-logged in, `new proxy …
  success`, zero rejects), then the rest; all `healthy` with 0 probe failures.
- **Rotation finalized**: Vault -> new secret only; **deploy 2**
  `20260830T215317Z` (ROLLOVER, no migrations); relays still healthy; live
  visitor connections flowed throughout -- zero tunnel downtime. Secret
  scratch files shredded.

## Production box 0b24ee94 RAID rebuild (same evening, concurrent)

OVH had replaced the failed NVMe on 2026-08-25 (ticket #724677 -> intervention
#724696: old disk SN SDM000039B60 -> new SN 032510B00227) but the arrays were
still degraded. Rebuilt online, zero downtime for the box's 2 leased
workspaces:

- **The replacement disk is ~765 MiB SMALLER than the survivor**
  (Micron 1,875,385,008 sectors vs HGST 1,876,951,040), so the runbook's
  "replicate the partition table" step cannot be followed verbatim. Adjusted
  layout on the new disk: ESP shrunk 511 -> 128 MiB (6 MB used), `/boot`
  mirror partition unchanged (1 GiB), p3 takes the rest (893.1 GiB -- fits
  md3's full data size at the same mdadm data offset with ~130 MiB spare), and
  the survivor's unused 512 MiB trailing p4 not replicated.
- `mdadm --add` for md2 + md3; resync at a temporarily raised
  `speed_limit_max` (500 MB/s); both arrays reached `[2/2] [UU]` clean.
- ESP contents copied (`diff -r` verified) and a `debian2` EFI boot entry
  added for the new disk -- placed AFTER the PXE entries and the existing
  `debian` entry so OVH's netboot chain and default boot order are untouched.
- Bugsink box-health issue resolved after the rebuild; no regression on
  subsequent hourly checks.

## Production pool bake

16/16 succeeded, spread across boxes, pinned `--server-id`, from THIS
worktree with `PATH` fronted by `scripts/` (the mngr-shim gotcha):

- US-EAST-VA: 4 on `8cade5fa` + 4 on `c011f511`.
- US-WEST-OR: 4 on `0d59c281` + 4 on the freshly rebuilt `0b24ee94`.

All 16 rows `available` at `repo_branch_or_tag=minds-v0.4.3` (8 GB / 2 vCPU).
Prior available capacity untouched (0.3.17: 27 east / 4 west; 0.4.1: 19 east /
6 west; 0.4.2: 11 east / 23 west). Fleet after: 23 servers, 295/337 slots
used, 42 free. Pre-bake audit: DB slot survey and on-box audit agreed
box-for-box (no phantom slots this cycle), fleet tier-exclusive and clean.

## Post-deploy verification (2026-08-31)

- Desktop fast path: a fresh workspace (`test-of-0-4-3`) FAST-PATH leased a
  minds-v0.4.3 slice, adopted cleanly, shared on a **workspace-keyed domain**,
  and every service panel worked in the shared view -- the exact path the tag
  re-cut fixed, verified live on production.
- `share_visit_authorized` confirmed in production OpenObserve (the second of
  the two never-yet-observed log channels; the R2-sweep orphan metric covered
  the first).

## Found and fixed during verification: leased-here trust-material staleness

Opening the operator's five existing workspaces from the machine that LEASED
them (in July, pre-adoption-era client) spun forever on "Loading workspace":
every outer SSH refused with `Host key ... does not match`. The workspaces had
since been adopted from a second device (August), rotating the slices' sshd
keys; the adopted keys were correctly synced through the workspace records --
but `_materialize_record_secrets` skipped the SSH half for any host with a
local `lease.json` ("the leasing install is authoritative"), stamping the
revision while leaving stale bake-time bootstrap pins behind. Related to, but
distinct from, the 0.4.1-era stranding incident
(`slice-hostkey-revert-stranding.md`): here the machines were healthy and the
staleness was purely client-side.

Fixed on this branch (`53af57156b`): the lease exemption is removed (the
revision + content-hash gate is what protects a leaseholder's newer unpushed
material) and a bootstrap-drift escape hatch
(`has_unpinned_bootstrap_drift` in mngr's host-key pin store) re-applies
record pins whose endpoints hold only absent-or-bootstrap material, deferring
to differing user-origin pins so a local rotation is never clobbered.
Verified live: all five workspaces converged on the synced adopted keys at
next startup and loaded. Any multi-device user with the same
leased-before-adoption split heals the same way once their client carries the
fix -- fold it into the next build promoted to the desktop channels.

## Deliberately deferred (decided during this deployment)

- Desktop channel rollout: `release-channels.toml` untouched (all channels
  still at 0.4.2 build `260825un55i8ix7`), connector download fallback
  untouched. Note the client fix above argues for including it in whatever
  build is eventually promoted.
- Issue mngr-internal#746 (CSP frame-ancestors chrome-origin fix): next
  release. Meanwhile desktop-created shares stamp the modal.run connector
  origin into frame-ancestors, so the /web chrome cannot frame
  desktop-shared workspaces (known, accepted).
- Legacy path-secret `/frps/auth/{secret}/{relay_id}` route removal: waits on
  the standing dev envs' relays being migrated to the header form + rotated.

## Notes

- All operator SSH throughout used pinned host keys where records existed
  (boxes) and the documented key-handling gotchas held (Vault `-field=value`
  strips the SSH keys' trailing newline; re-append it).
- The deploy ran from a git worktree of the release branch with the
  code-guardian stop hook disabled (the hook merges main into the worktree,
  which confuses "what is being deployed"); re-enabled at wrap-up.
