# Handoff: slice-fleet — finish the canary testing phase (specs/slice-fleet phase 2)

Written 2026-08-24 by the agent that ran the autofix pass over the variable-machine-sizing work and executed the SIZING portion of the dev canary (specs/slice-fleet phase 2), fixing the five bugs the canary caught live.
Audience: the next agent finishing the canary phase — the gen-1 -> gen-2 conversion, drain, management-plane activation, and telemetry hand-verification — and then moving toward phase 3 (CI split fleet).
Read this alongside, and do not skip:

- **The spec** (single source of truth): `specs/slice-fleet/spec.md` — "Slice fleet: variable machine sizing and the gen-2 completion". It SUPERSEDES `specs/slice-fleet-gen2/spec.md` (which keeps a pointer header and remains the historical record of the gen-2 design and its phase 1-4 decisions — still worth reading for the mechanics). Phase numbering in this doc follows the NEW spec: phase 1 = variable sizing (DONE, dev-verified), phase 2 = dev canary (sizing portion DONE, the rest is your job), phase 3 = CI split fleet, etc.
- **The prior handoffs** (mechanics, decision logs, traps — all still apply):
  - `~/handoff/new-fleet-phase-4.md` — the canary box's full biography, phase-3 (management plane) machinery, phase-4 (telemetry) work list, the activation runbook pointers.
  - `~/handoff/new-fleet-phase-3.md` — phase-2 mechanics (stop/start, transfer scripts, conversion design, the `&&`-join and reserve-idempotence traps, connector fakes).
  - `~/handoff/new-fleet-phase-2.md` — phase-1 mechanics (qemu unit/helper/sudoers, nftables/tc rationale), vault/credential paths, the decision log from Josh's Q&A.
- The persistent memory file `new-fleet-testing-progress` in the agent memory dir (short summary of branch + canary state).

## Where things stand right now

- **Branch:** `mngr/new-fleet-testing` (worktree `~/.mngr/worktrees/new-fleet-testing-8219ade4acea43c787905abce25003d6`), HEAD `52871fa501`, pushed.
- **PR:** imbue-ai/mngr-internal **#573** (draft), **base = `mngr/variable-sizing`** (STACKED on PR #571 — never diff/review against main; always pass `base = origin/mngr/variable-sizing` to review agents). PR body describes everything.
- **Branch contents** (all relative to `origin/mngr/variable-sizing`):
  1. The 10 `mngr/continue-new-fleet` commits: the full variable-sizing implementation (sizing model/constants, gen-2 renderers with the 512-ordinal ceiling and placeholder-substituted templates, in-guest oneshots, connector migration 033 + `machines.py` resize router + two-budget accounting + two-pass eviction + lazy disk backfill, wire fields, `mngr imbue_cloud machines show/resize` CLI, `pool create --units`, ordering units-valid guard, docs, `test_machine_resize.py` release test).
  2. 8 autofix commits (4 unattended iterations): missing `libs/mngr` changelog entry; start-path units-quota re-read under the user advisory lock; boot-disk included in `server list` disk accounting; lease-path quota tests; crashed-machine disk-quota subtraction guard; stale disk-target clamps on BOTH apply paths (`max(base, target)` in the sizing helper, `GREATEST` in the in-place restamp CAS); admin-resize serialized against starts via the owner lock.
  3. 5 canary-caught fix commits (see the bug list below) + changelog renames + 2 test-only commits from the final focused autofix pass.
- **Changelog entries are named `mngr-new-fleet-testing.md`** (renamed from `mngr-continue-new-fleet.md` — the gate keys entry names on the PR branch). If the PR branch ever changes again, the entries must be renamed again.
- **Recorded deliberate non-fixes** (in `.reviewer/outputs/autofix/unfixed/*.jsonl` on the branch — BUILD around them, don't "fix"): resize.sh debris left in the box transfer dir; ambiguous friendly-name match in `cli/machines.py`; `created_at`-order (not largest-first) eviction planning; the slot-based FLEET summary line in `server list`; candidate boxes missing `ram_gb` abort the restore loop loudly; `_format_gen2_capacity` would raise on a degenerate `ram_gb <= 8` row; the `/account` usage pass-through fields have no direct test.
- **Known deliberate gap:** the spec's read-only desktop-client machine-size display is NOT on this branch (flagged as a follow-up; do not add it silently to this PR).
- **DWT worktree:** `.external_worktrees/default-workspace-template` on branch `mngr/new-fleet-testing` (fresh from origin/main; NO changes made or needed so far). TRAP from earlier phases still applies: `git fetch origin main:main` there before trusting any diff; never commit `system/vendor/mngr`.
- **CI:** the stop hook polls PR #573. Scoped local suites all green at handoff (`just test-quick` per project: remote_service_connector 833, minds_admin 532, mngr_imbue_cloud 557/558 — 0 failed). The full suite runs in CI only (Josh's standing rule: never run the full suite locally).

## The five bugs the canary caught live (all fixed on the branch — know them, they shape the tips below)

1. **`minds-admin server prep` died with "Argument list too long"** (`0c56e7dd35`): the gen-2 prep script (512 per-ordinal sudoers grants) pushed the old base64-in-ssh-argv form past the kernel's per-argument limit (MAX_ARG_STRLEN, 128KiB). `run_root_script_over_ssh` (apps/minds_admin/cli/server.py) now scp's the script to a random `/tmp/mngr-box-script-<hex>.sh` on the box under the pinned host key and runs it by path (exit status preserved, file always removed).
2. **Every gen-2 bake died on a bash syntax error** (`1d8bc62054`): `QemuSliceVpsClient` prefixed remote commands with a bare `PATH=... <command>` assignment, which cannot precede a compound statement — and the gen-2 disk listing is a `for` loop. Now `export PATH=...; <command>` (qemu_slice_client.py), pinned by a `bash -n` test of the actual remote string.
3. **The first carve failed on the dpkg lock** (`ad3508de4e`): a gen-2 guest's sshd answers while first-boot cloud-init is still apt-installing, and the bake's outer provisioning raced it. `slice_provider.py` now runs `cloud-init status --wait` (600s, degraded state logged-and-tolerated) over the VM's root SSH before ANY outer step, gen-2 only (`limactl start` already blocks for gen-1).
4. **A recorded disk grow never resized the qcow2, and the measured-size backfill recorded garbage** (`839fd75ba1`): qemu 10's `qemu-img info --output=json` nests child (file protocol) nodes carrying their own `virtual-size`; the whole-document grep captured several numbers; the in-place resize script's integer guard went silently false (if-conditions are exempt from errexit) and the upload's status values failed the connector's `.isdigit()` into None. All three parses in `box_scripts_gen2.py` now read the top-level key via `python3 -c 'import json,...'` (python3 is part of gen-2 prep — the telemetry collector needs it).
5. **"Not possible even after eviction" for a size the box easily held** (`803f18b859`): `_plan_eviction_for_box` (stop_start.py) counted the restoring machine's OWN pool row into the box occupancy sum. Self is now excluded (the reserve reclaims the same-instance dir; the new footprint is exactly the needed size).

General lesson behind 1/2/4: **the rendered-bash unit tests never execute the pipelines against real tool output** — `bash -n` catches syntax, not semantics. When touching box scripts, run the actual pipeline against real qemu/ssh output shapes (see `test_gen2_virtual_size_parse_reads_the_top_level_key_only` for the stub-on-PATH pattern).

## What the canary DID verify (sizing portion of phase 2 — done, don't redo)

All live on the canary box through the real flows, 2026-08-24:

- Box re-prep converged the branch's artifacts: 512 slice users, 512-line sudoers (`visudo` OK), units-aware helper, wg key intact.
- **First-ever gen-2 carve** — the whole reasoning-derived per-VM ruleset is now ground truth: DNAT on both forwarded ports (counters moving), output-hook DNAT, masquerade + loopback hairpin (used by the image-cache transfer during the bake), guest→box input drop counting, anti-spoof rule, **SMTP-25 block verified from inside the guest with the `slice_0_smtp_blocked` counter incrementing while 587 stayed open**, ct ceilings present, per-VM named counters, fw-mark, HTB class exactly `uplink x units/total` with ceil = full uplink (66Mbit at 8/120, 133 at 16/120, 933 at 112/120).
- Sizing env schema on-box (`MNGR_SLICE_UNITS/TOTAL_UNITS/MEMORY_MIB/DATA_DISK_GIB/VCPUS`), both in-guest oneshots enabled and working (btrfs fs grow to the new size at boot; container memory cap follows the VM's VISIBLE RAM minus 1GiB — note it tracks visible, not advertised, so expect e.g. 7516192768 at 8 units).
- Lease response carries the bake-stamped sizes; `mngr imbue_cloud machines show` renders current/target + `is_restart_needed_to_apply`; headless `auth signin` works against the dev connector.
- Refusals: free-plan units quota 403 (structured `quota_exceeded`), allowed-size 400 (`must be a multiple of 8 between 8 and 128`), disk-shrink 400, gen-1 row 409 ("not resizable ... becomes resizable once it migrates"), `starting`-state refusal untested live but integration-covered.
- In-place resize UP (8->16 + disk 28->56 recorded) and DOWN (16->8 + disk grow to 84): VM RAM, vCPUs (floor-proportional with the thread cap: 4 at 16 units, 16 at 112), HTB, container cap, fs grow — all land after stop/start; connector restamps current=target and clears targets; ports/placement unchanged.
- Stop/start round trips on gen-2 (upload + verify was fast for a thin workspace; the retention-window restart-in-place path).
- **Resize-via-restore with two-pass eviction**: 112-unit target refused in place (NO_UNITS), the available row was CAS-evicted (row deleted, VM torn down on the box), the reserve re-ran and the machine restored at 112 units with its 84G data intact.
- **Placement-impossible**: 128 units failed the start cleanly back to `stopped` with `transition_error` = "this machine size is not possible right now (no box can fit it, even after eviction); try a smaller size or try again later", data intact, target preserved; a later corrected target (8) started fine from that state.
- **Quota-lowered-after-stamp**: an admin-stamped 112-unit target with an 80-unit account cap refused the user's start 403, machine stayed `stopped`, targets intact (the spec's edge case, hit accidentally and verified).
- Release: `POST /hosts/{id}/release` tore down the VM (0 instances on the box) and removed the row.
- The two machine-sizing entitlements + usage fields are live in `/account` for dev (seeded by env deploy from the tier deploy.toml `[plans]`).

**NOT visually confirmed:** the `pool_rows_evicted` / `machine_resize_placement_impossible` / `machine_resize_recorded` / `machine_disk_backfilled` metric lines in the dev OpenObserve (the code paths ran; nobody looked at the dashboards). Cheap to check while doing the telemetry item below.

**Weakly tested:** the lazy `disk_gb` backfill for NULL-disk rows — our row was bake-stamped, so only the integration tests cover the NULL path. Note the backfill was BROKEN until fix 4 (it recorded None), so no real row has ever exercised it; the conversion test below creates the perfect specimen (a converted gen-1 row has NULL `disk_gb`).

## Environment state you inherit

- **The canary box** (unchanged coordinates): `debian@51.81.208.81`, dev-josh-2 `bare_metal_servers` id `03a9a4af-a5c8-47e7-b2d9-5f5dae4e08fe`, hil/US-WEST-OR, gen 2, status `ready`, 120-unit / 765G budgets, prepped at branch HEAD (scp-based prep). Renews ~Sept 20.
- **On it right now:** ONE fresh available pool row baked from this branch: host `host-b1441c44a5f7404d8cde5aaaa2857b4a` (row `slice-5a2cd405ef5e4b4284320bb108260e99`, ordinal 0... verify with `just list-pool-hosts`; ports vm=22002/container=22003, 8 units / 28G, `repo_branch_or_tag=mngr/new-fleet-testing`). Lease it or leave it for real use.
- **dev-josh-2 is deployed at branch HEAD minus the last few commits** — the connector was last deployed AFTER fix 5 (`803f18b859`); the two test-only commits since don't affect runtime. If you change ANY connector/plugin/minds_admin runtime code, redeploy before live-testing: the connector renders all box scripts server-side, so box behavior follows the DEPLOYED code, not your checkout. Deploy: `eval "$(uv run minds-admin env activate --deploy dev-josh-2)" && MINDS_WEB_TEMPLATE_REF=mngr/new-fleet-testing uv run minds-admin env deploy` (the WEB_TEMPLATE_REF is mandatory for dev deploys and must match the pool rows' baked ref).
- **Migrations:** dev-josh-2's pool DB has through 033. OTHER dev envs still lag until their next deploy (`UndefinedColumn` on `memory_units` = that env needs a deploy; `minds-admin server list` now touches the new columns, so even read commands fail on a lagging env).
- **Test account:** `canary-sizing-4b0f3e59@imbue.com` / password `canaryPW-9271!` (created via `/admin/test-signup`, pre-verified, plan=ally, `max_active_machine_units` admin-bumped to **128** — disposable; reset or ignore). Access tokens expire ~1h; re-`/auth/signin` for a fresh one.
- **Gen-1 state:** the single gen-1 leased dev row (`2e379613-ee40-4a7d-9f31-23d8bb5cb8ce`, host-dd03c7fb..., on vin box e1396039 = 15.204.140.221) is **Josh's live workspace — NEVER touch it**. There are currently NO other gen-1 dev rows.
- **Credentials** (the session scratchpad I used is ephemeral — refetch): dev vault token `~/.vault-token` (**expires ~Aug 28 — re-login soon**: `vault login -method=oidc`), `VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200`, `VAULT_NAMESPACE=admin`. Pool key: `vault kv get -format=json secrets/minds/dev/pool-ssh/POOL_SSH_PRIVATE_KEY` (NOTE the layout: each key is its own nested entry with a single `value` field — `vault kv get -field=X secrets/minds/dev/pool-ssh` does NOT work). Admin key: `secrets/minds/dev/supertokens/MINDS_ADMIN_KEY` (same nested-entry shape). Pool DSN: `~/.minds-dev-josh-2/secrets.toml` `[secrets] NEON_HOST_POOL_DSN`. Box host key: pin from `bare_metal_servers.box_host_public_key` (never TOFU for the box; TOFU is acceptable for VM probes of a machine you just leased, per the release test's precedent). Connector: `https://minds-dev-dev-josh-2--rsc-dev-api.modal.run/`.

## REMAINING WORK — finish the canary phase

The spec's phase-2 checklist minus what's done above. In rough order:

### 1. Gen-1 -> gen-2 artifact conversion with the strict dist-upgrade (the big one)

The conversion path (phase-2-of-gen2 machinery: conversion cidata, one-time replay, data-disk remount at the lima mount point, detached in-VM bookworm->trixie dist-upgrade, supervisor-issued reboot, trixie verification, `box_generation` restamp) has NEVER run against a real gen-1 disk. It needs a **sacrificial gen-1 workspace**:

- Bake one gen-1 slice for yourself: `just bake-slice-dev US-EAST-VA "" 1 --server-id e1396039-1a7f-4583-b5ab-03d7ad47553d` (the 6-slot vin box, 5 free) or the 14-slot `1151d45f-...` box (135.148.34.234, currently empty of dev-josh-2 rows). The bake dispatches on the BOX generation, so a gen-1 box yields a lima slice automatically.
- Lease it with the test account, put some marker data in the workspace (a file in the container's home; ideally also verify the chat agent boots if you want "workspace use" coverage), stop it (gen-1 stop = lima artifact upload).
- Start it. Restores prefer `box.box_generation >= row.box_generation`, and BOTH generations of box are candidates for a gen-1 artifact — to force the conversion deterministically, the gen-1 origin box's retention fast-restart must not win: stop, wait for `stopped`, then either wait out/destroy the retained local VM or (cleaner) `minds-admin server drain` the gen-1 box (see item 2 — the two tests compose: drain forces the restore onto gen-2 WITH conversion).
- Verify per the old spec's open questions: netplan applied by the one-time replay (the VM answers on its NEW ordinal-derived /30), data disk remounted at `/mnt/lima-<disk>` with the marker data intact, `host_dir` symlink resolving, host key byte-identical (adoption re-verifies), guest reports `VERSION_CODENAME=trixie`, container back up, **upgrade duration** (measure it — the 1-hour poll bound is an open question), and the row restamped `box_generation=2` with `attributes.guest_upgrade_failed` clear.
- **Sizing tie-ins to verify on the same run:** the converted row's sizes stamp from measurement (spec: "Gen-1 -> gen-2 conversion restores stamp the machine's actual measured sizes"); its `disk_gb` starts NULL -> the df guard sizes from the artifact's manifest bytes -> the FIRST gen-2 stop afterwards exercises the (newly fixed) lazy backfill for real. Also confirm the converted VM (which predates the sizing oneshots) gains `mngr-grow-data-fs` + `mngr-reconcile-container-memory` via the conversion user-data (`render_gen2_conversion_user_data` includes them — verify enabled in the guest), covering the spec's "pre-sizing gen-2 VMs" open question for the conversion path.
- After success: resize the converted machine and restart to prove a converted workspace is resizable end to end.

### 2. Drain of a gen-1 dev box

`minds-admin server drain --server-id <id>` on whichever gen-1 box hosts ONLY your sacrificial workspace (use the empty 14-slot `1151d45f` box... it has no leased rows, so to make the test meaningful bake your sacrificial slice THERE, or drain `e1396039` — **refuse this while Josh's workspace lives on it** unless Josh says otherwise; force-stopping his workspace is exactly what drain does). Verify: status -> `draining`, unleased rows destroyed, leased rows force-stopped, restores excluded from the draining box (candidate listing filters `status='ready'`), and re-run idempotence. **Set the box back to `ready` afterwards** (`minds-admin server set-status`) — do not leave a dev box draining.

### 3. Management-plane activation (wg + :22 lockdown) — the phase-3 exit criteria, still unexercised

Operator actions, full runbook `apps/minds/docs/deploy/gen2-management-plane.md` + the numbered list in `~/handoff/new-fleet-phase-4.md` §"To ACTIVATE":
1. Create the Modal Proxy in the minds-dev workspace (dashboard; Team plan: 1 proxy/workspace, <=5 static IPs — ALL dev envs share it).
2. Fill in `apps/minds/imbue/minds/config/envs/dev/management_plane.toml` (currently the commented scaffold; since merged into `deploy.toml` as the `[management_plane]` table): Josh's wg PUBLIC key as an operator (ask him to run `wg genkey | tee wg.key | wg pubkey`, address e.g. 10.202.0.2) + `[modal_proxy]` name/static_ips. Commit it (all public values).
3. Redeploy dev-josh-2 (threads `MINDS_CONNECTOR_MODAL_PROXY_NAME` into the connector) and verify a lease/stop cycle STILL WORKS before any lockdown (the proxy egress must be allowlisted before :22 closes). This also answers the standing open question: does paramiko egress actually leave via the proxy IPs.
4. Re-run `minds-admin server prep --server-id 03a9a4af-...` — installs the :22 lockdown + operator peer on the canary box.
5. `minds-admin wg config` -> splice private key -> `wg-quick up` -> SSH `debian@10.202.1.1`. Exit criteria: box :22 unreachable except from the connector; operators over wg. Rollback = empty the toml's `[modal_proxy]` + re-prep (converges the box to open).
Ordering matters (3 before 4; operator committed before the prep that drops laptop :22).

### 4. Telemetry hand-verification (the phase-4 exit criteria on real traffic)

- Per-tap hostmetrics: with a slice running, confirm `mslice<N>` interface metrics appear in the dev OpenObserve (`https://telemetry.minds-dev.com/` per the prep log).
- The nftables counter collector: confirm per-slice counter JSON (`slice_N_egress/ingress/new_connections/smtp_blocked`) flows via journald with the host_id label; chart `smtp_blocked` (it has real nonzero data from the canary now).
- **The sudo-anomaly signal vs the new 512-grant sudoers**: `bounded the sudo-anomaly allowlist to the granted slice ordinals` (commit `3826dfa3f1`) predates GEN2_MAX_SLICE_COUNT going 64 -> 512. Verify on the box that legitimate `systemctl start/stop mngr-slice@<high ordinal>` sudo entries do NOT trip `MNGR_BOX_SIGNAL sudo_anomaly` — and that the prep-artifact integrity check passes against the new (scp-shipped) artifacts. Read `apps/minds_admin/.../slices/box_telemetry.py` (or wherever the collector renderer lives now) and the integrity-check runbook first.
- The alert rules: dev's GitHub-webhook destination still carries a placeholder token (arming = re-run `provision-alerts` with an issues-write PAT — Josh gates this; ask). At minimum confirm the alert RULES exist and the metric lines from the eviction/placement tests are queryable.
- Check the new sizing metrics landed: `machine_resize_recorded`, `pool_rows_evicted` (1 from the canary), `machine_resize_placement_impossible` (2), `machine_disk_backfilled` (should be 0 so far — broken parse; expect its first real hit from the conversion test).

### 5. Odds and ends before calling phase 2 done

- **Run the release tests against dev**: `just test apps/minds/deployment_tests/test_machine_resize.py::test_machine_resize_applies_units_and_disk_grow_at_restart` with `MINDS_MACHINE_RESIZE_RELEASE_TEST=1` (and the migration release test alongside it, per the spec's testing strategy — these do NOT run in CI, so they must be proven locally; the deployment-tests orchestrator README is `apps/minds/deployment_tests/README.md`). I verified the identical steps by hand but never ran the test files themselves — they're new on this branch and unproven as TESTS.
- **CI green on #573** — watch the stop-hook/CI. Known-flaky Modal acceptance tests (`test_exec_echo_on_modal` etc.) just get re-run.
- **Merge sequencing**: #571 (variable-sizing, the blueprint) -> #573. Verify GitHub retargets #573 to main when #571 merges. NEVER rebase.
- The `machine_disk_backfilled`-vs-clamp interplay after the conversion test: the canary machine currently has `disk_gb=84` stamped while its life ended (released); nothing dangling. But note for debugging: mid-canary there was a WINDOW where DB said 56 and the physical qcow2 was 28G (fix-4's bug) — if you see similar skew on a future row, the start-time clamps handle it; don't hand-edit the DB.
- Keep notes for `gen2-turnover.md` (phase-5 doc deliverable) as you go — especially conversion duration and drain ergonomics.
- The desktop-client read-only size display (spec phase-1 item) — a small follow-up PR against the minds app; the wire fields it needs are already served.

## Tips, traps, and conventions (new this session — earlier handoffs' lists all still apply)

- **The connector renders box scripts server-side.** Live-testing a connector change ALWAYS requires `env deploy` first (~4 min). A dev deploy is RECREATE strategy — brief downtime, safe while machines run (the supervisor is re-driven).
- **Dev deploys demand `MINDS_WEB_TEMPLATE_REF`** matching the pool's baked `repo_branch_or_tag` (currently `mngr/new-fleet-testing`) — the deploy refuses without it.
- **Bake commands**: `just bake-slice-dev <REGION-LABEL> "" 1 --server-id <id>` (region label = `US-WEST-OR` for hil, `US-EAST-VA` for vin — the LEASE label, not the datacenter code). The DWT worktree is picked up automatically. `--units N` for odd-size dev bakes. A gen-2 bake takes ~10-15 min (image built in-guest; deferred install skipped).
- **Driving the connector directly** (the release test's pattern) is far faster than the full mngr client path for lifecycle testing: `/admin/test-signup` -> `/auth/signin` -> `/hosts/lease` (needs an ssh_public_key; mint an ed25519 pair) -> `/machines/{id}/resize` -> `/workspaces/{id}/stop|start` (poll GET `/workspaces/{id}`) -> `/hosts/{id}/release`. Stops/starts each take 1-4 min on the canary for thin workspaces.
- **VM-root SSH for verification**: the pool key is authorized on every baked VM root (`ssh -i <pool_key> -p <vm_port> root@51.81.208.81`). Box SSH as `debian` with the pinned host key; the storage tree needs sudo to list.
- **`qemu-img info` on a RUNNING VM's disk fails with a lock error** — use `-U` (force-share) for read-only probes.
- **Container memory caps track VISIBLE guest RAM** (MemTotal - 1GiB), which is ~2-4% below the advertised size — don't "fix" apparent off-by-a-few-percent caps.
- **Access tokens expire in ~1h** — long poll loops should re-signin or tolerate a `KeyError: 'status'` tail (that's a 401 body).
- The admin quota-bump response is the admin account view — its shape differs from `/account`; don't parse `.entitlements` blindly.
- **Watch out for stop-hook autofix re-fires**: every commit block on this branch re-triggers the gate. Focused passes (pass the last verified HEAD + a delta description, while keeping base = `origin/mngr/variable-sizing`) are explicitly allowed and much cheaper. Feed every fresh agent the settled-decisions list or it re-litigates.
- The scratchpad/`/tmp` artifacts from my session (pool key copy, tokens, logs) are session-scoped — do not depend on them; refetch from vault.
- `eval "$(uv run minds-admin env activate dev-josh-2)"` per shell invocation (the Bash tool doesn't persist env between calls); `--deploy` variant for deploys.

## Verification commands used (all green at handoff)

- Scoped suites: `just test-quick "apps/remote_service_connector -m 'not acceptance and not release and not modal and not docker and not docker_sdk and not tmux'"` (833 passed), same for `apps/minds_admin` (532), `libs/mngr_imbue_cloud` (557/558) — 0 failed everywhere; ratchets included.
- Changelog gate: `GITHUB_HEAD_REF=mngr/new-fleet-testing GITHUB_BASE_REF=mngr/variable-sizing python3 -m scripts.check_changelog_entries` — ok, 10 projects.
- Live canary re-verification is cheap: `just list-servers` (canary shows `0/120u` used after my cleanup... actually `8/120u` with the fresh bake — one available row), `just list-pool-hosts`, box SSH checks per the phase-4 handoff §Verification, and `sudo nft list table inet mngr_slices` for the live ruleset whenever a slice runs.

---

# Addendum: second canary session (2026-08-24, PR #574) -- state and the agreed follow-up work

Written by the agent that finished the canary phase (the section above this line is the FIRST session's handoff; its environment details are superseded by this addendum where they conflict). Everything in the "REMAINING WORK" checklist above is DONE and verified live -- conversion (with resize of the converted machine), drain, management-plane activation, telemetry -- plus the release tests now exist and pass against dev. PR #574 (base `mngr/new-fleet-testing`) carries the session's fixes; read its description for the full list. The dev tier's alert delivery is ARMED (real token, proven end to end).

## Environment deltas vs the first session's notes

- The canary box's `:22` is now LOCKED DOWN: it answers only the Modal proxy egress IP (34.239.15.6) and the WireGuard overlay. Management SSH goes over WireGuard (`debian@10.202.1.1`); Josh's operator private key is at `~/.minds-dev-wg/wg.key` on his dev box, client config staged at `~/.minds-dev-wg/mngr-dev.conf`. The per-slice port range (22000+) stays open.
- The Modal proxy is `minds-dev-connector` in the minds-dev workspace, Modal environment `main` (lookup is environment-scoped; the workspace's proxy-IP limit is 1). Committed in the dev `management_plane.toml`.
- The alerts GitHub token (`mngr-openobserve-alerts`, fine-grained, Issues read/write on mngr-internal only, EXPIRES 2027-08-24) lives at `secrets/minds/dev/observability/OBSERVABILITY_ALERTS_GITHUB_TOKEN` and inside the dev OpenObserve destination.
- Pool state: two fresh available gen-2 rows on the canary; the sacrificial/converted test workspaces are released. Both gen-1 boxes are `ready`.
- The test account, Vault paths, and box coordinates from the first session's notes are unchanged and still work.

## Agreed follow-up work (do these next; each was discussed and settled with Josh)

### 1. Convert "wg" to "wireguard" across code, docs, and variables

Motivation: the no-abbreviations style rule applies to prose AND identifiers; "wg" leaked everywhere during the management-plane work.

- Prose first (cheap, no compat concerns): docs (`gen2-management-plane.md`, `gen2-telemetry.md`), the blueprint notes, template comments, changelog entries, docstrings and comments in `management_plane.py` / `wg_admin.py` / `box_telemetry.py`.
- Identifiers: rename python variables/functions/settings (`wg_address` -> `wireguard_address`, etc.), the CLI surface (`minds-admin wg ...` -> `minds-admin wireguard ...`; decide whether to keep `wg` as a hidden click alias for muscle memory), and the `--via-wg` flag.
- DB columns `bare_metal_servers.wg_address` / `wg_public_key`: needs a migration (034+). Additive-rename pattern: add the new columns, backfill, keep the old ones readable until the deploy settles, then drop (mark with CLEANUP:).
- Do NOT rename things that are external conventions: the `wg`/`wg-quick` binaries, `wg0` interface name, `/etc/wireguard/` paths, and `[Interface]`/`[Peer]` config syntax are WireGuard's own surface.

### 2. Credential-expiry reminder job (and make it the reusable mechanism)

Motivation: the alerts GitHub token silently dies at expiry (the webhook just starts 401ing inside OpenObserve; alert rules keep "firing" into a dead destination); GitHub's own reminder email is personal and a week out.

- Add a committed registry, e.g. `.github/credential-expiries.toml`: entries of `name`, `expires_on` (ISO date), `rotation` (one-line pointer). First entry: `mngr-openobserve-alerts`, `2027-08-24`, rotation = mint a replacement fine-grained PAT (Issues read/write on mngr-internal only), update `secrets/minds/<tier>/observability/OBSERVABILITY_ALERTS_GITHUB_TOKEN` in EVERY tier's Vault entry, and re-run `provision-alerts` against every ARMED tier's instance. Consider adding the OpenObserve origin TLS certificates as further entries.
- Add a weekly GitHub Actions cron workflow (`.github/workflows/credential-expiry-reminder.yml`) that reads the registry and files an issue for any entry within 30 days of expiry. Idempotent: search open issues by an exact title (e.g. "credential expiring: <name>") before creating. Uses the workflow's own `GITHUB_TOKEN` (issues: write on its own repo) -- no new secrets.
- Document the mechanism (a short section in the registry file's header comment is enough) so future expiring credentials get added instead of new ad-hoc reminders.
- This is `dev/`-project work (`.github/`), so its changelog entry goes in `dev/changelog/`.

### 3. Generation on the wire (lease filter)

Motivation: pool rows are attribute-identical by design, but during any mixed-generation window (turnover, the CI split fleet) tests and operators need to lease a SPECIFIC generation; today the only way is lease-and-release-until-lucky, which destroys baked rows.

- Model it like `region`, NOT like the attributes document: `box_generation` is a column with a lifecycle (conversion restamps 1 -> 2), so an attributes mirror would be a second copy every conversion CAS must maintain. Add an optional exact-match request field on `POST /hosts/lease` (absent = unconstrained; additive wire change, old clients unaffected) filtered against the column in the lease query. The lease RESPONSE already returns `box_generation`.
- Plugin side: a `-b generation=N` build arg (parse in `parse_imbue_cloud_build_args`, validate 1/2, thread into the lease request).
- Then simplify `apps/minds/deployment_tests/test_machine_migration.py`: replace `_lease_an_eligible_gen1_machine`'s retry loop with one generation-filtered lease (the docstrings there point at this item).
- Tests: connector lease-filter unit/integration tests + wire-compat (additive), plugin parse tests.

### 4. WireGuard in the bake/prep tooling

Motivation: `pool create` (bakes) and `server prep` SSH the box's PUBLIC `:22` from the operator machine, which the lockdown drops -- on dev this was sidestepped by baking before locking down; production turnover (phase 4) drains/repaves/bakes against locked-down boxes routinely. Settled design (with Josh): the tooling uses the overlay; the Modal-sandbox bounce host remains break-glass only.

- Build one shared resolver in `minds_admin`: "management address for this box" = the row's `wg_address` when set AND a quick reachability probe (sub-second TCP dial to `:22` on the overlay address) succeeds, else `public_address`. Automatic -- no flag to forget; first-ever prep (no overlay identity, but no lockdown either) falls out correctly; non-overlay operators fall back after one fast probe.
- Thread it through every operator-side box-`:22` call site: the bake (`cli/pool.py` -> the `-S providers.imbue_cloud_slice.box_public_address=...` threading), `server prep`/`setup`, `server drain`'s destroys, `pool destroy`/`teardown-slices`, `audit-boxes` (`--verify-occupancy`), `backfill-host-keys`, `repair-keys`. Host-key pinning is by KEY not address, so swapping the dialed address is transparent to trust. The slice VM/container ports (22000+) stay on the public address -- they are not locked down.
- The connector's paths need nothing (already proxy-allowlisted).
- Unit tests on the resolver; live verification is a canary exercise (CI cannot reach the overlay). Land this BEFORE the first staging lockdown.
- Already reproduced concretely on dev (2026-08-24): the post-lockdown pool restock bake failed with `ssh: connect to host 51.81.208.81 port 22: Connection refused`, so the canary's pool CANNOT be restocked until this lands -- it is the next session's first item, not just a phase-4 nicety. (The dev pool currently has NO available rows for the same reason.)

## Also queued (from the canary summary, acknowledged by Josh)

- The desktop-client read-only machine-size display (spec phase-1 item; wire fields already served) -- a small separate PR against apps/minds.
- Arming staging/production alerts at their bring-up: copy the SAME token value into each tier's `OBSERVABILITY_ALERTS_GITHUB_TOKEN` Vault leaf and run `provision-alerts` against that tier's instance (all tiers post to the same repo, so one shared token).

---

# Addendum 2: the agreed follow-up work landed (2026-08-24, third session)

All four items from "Agreed follow-up work" above are implemented on branch
`mngr/slice-fleet-canary-followups` (stacked on `mngr/finish-new-fleet-canary-testing`):

1. **wg -> WireGuard conversion** -- prose, identifiers, the CLI (`minds-admin wireguard`,
   with `wg` kept as a hidden alias; `--via-wireguard`), and the DB columns via the
   additive-rename migration 034 (`wireguard_address` / `wireguard_public_key`,
   old columns dual-written + COALESCE-read until every tier's pool DB has migrated;
   grep `CLEANUP:` for the drop conditions). The historical gen-2 spec and migration 032
   keep their original names on purpose.
2. **Credential-expiry reminder** -- `.github/credential-expiries.toml` +
   `.github/workflows/credential-expiry-reminder.yml` (weekly, idempotent by exact issue
   title, `scripts/credential_expiry_reminder.py`); first entry is `mngr-openobserve-alerts`.
3. **Generation on the wire** -- optional exact-match `box_generation` on `POST /hosts/lease`
   (NULL rows count as gen 1), `-b generation=<n>` in the plugin, and
   `test_machine_migration.py` now leases through the filter (retry loop gone).
4. **WireGuard in the bake/prep tooling** -- `slices/box_access.py` resolves every
   operator-side box `:22` dial to the overlay address when a sub-second probe reaches it,
   else the public address; threaded through bake/prep/drain/destroy/audit/sweep/
   warm-cache/repair-keys/backfill-host-keys/sync-peers. This unblocks restocking the
   canary's pool post-lockdown (bring the operator tunnel up first: `wg-quick up` the
   config staged at `~/.minds-dev-wg/mngr-dev.conf`).

Still queued (unchanged): the desktop-client read-only machine-size display, and arming
staging/production alerts at their bring-up.

## Addendum 2b: overlay renumbering + userspace transport (same session, after discussion with Josh)

Two design changes agreed after reviewing item 4's operational shape:

- **Per-tier overlay allocations** from a reserved `10.64.0.0/10` supernet (production
  `10.64.0.0/11`; staging `10.96.0.0/16`; ci `10.104.0.0/16`; dev `10.112.0.0/16`; each small
  tier at the base of a reserved /13 for one-value 8x growth; `10.120.0.0/13` spare). Chosen to
  avoid the Tailscale/CGNAT range (in use on operator machines) and to let several tiers'
  tunnels coexist on one machine. The table lives in the minds config
  (`MANAGEMENT_OVERLAY_CIDR_BY_TIER`); Josh's dev operator address is now committed as
  `10.112.0.2`.
- **onetun userspace transport**: box-management SSH now prefers a per-box userspace WireGuard
  forward (no root, no interface, no local peer list; peer built from the DB row per dial;
  verified by the box sshd's banner through the tunnel), falling back to a kernel-route overlay
  dial, then the public address. Requirements on an operator machine: the `onetun` binary
  (PATH or `MNGR_ONETUN_PATH`) and the private key at `~/.minds-wireguard/<tier>.key` (or
  `MINDS_WIREGUARD_PRIVATE_KEY_PATH`). `wg-quick` remains only for interactive box SSH; the
  rendered operator config now uses a `/32` Address.

**Canary renumbering runbook (the next operator session's first item, BEFORE the restock
bake):** the canary box's live `wg0` still has the OLD plan (box `10.202.1.1`, Josh's peer at
`10.202.0.2`), and its row renumbers to `10.112.1.1` at the next prep. The sequence that
converges it:

1. Copy the operator key to the new convention: `mkdir -p ~/.minds-wireguard && cp
   ~/.minds-dev-wg/wg.key ~/.minds-wireguard/dev.key` (and install onetun).
2. Bring up the OLD kernel tunnel (`~/.minds-dev-wg/mngr-dev.conf`, which still matches the
   box's live config) and run `minds-admin server prep --server-id 03a9a4af-...`. The prep
   restamps the row to `10.112.1.1` and rewrites `wg0.conf` (new address + Josh's new peer
   entry); expect the prep's own session to DROP when `wg0` restarts mid-run.
3. Re-run the same prep. The dial resolver now takes the userspace tunnel with the NEW
   identities (source `10.112.0.2` -> box `10.112.1.1`) and the idempotent prep completes.
4. Tear down the old kernel tunnel; re-render the client config (`minds-admin wireguard
   config`) if interactive SSH is wanted. Then the restock bake (`just bake-slice-dev ...`)
   exercises the whole transport end to end.

---

# Addendum 3: onetun pinned + installed, flag cleanup, and the live canary restock (2026-08-25, PR #609)

The three work items from `~/handoff/new-fleet-onetun-test.md` are done, on branch
`mngr/onetun-install-and-canary` (PR #609, stacked on #581):

1. **onetun pinned + installable**: `ONETUN_VERSION = "0.3.10"` with per-platform
   sha256s (`apps/minds_admin/imbue/minds_admin/slices/onetun_install.py`);
   `minds-admin wireguard install-onetun` downloads/verifies/installs to
   `~/.minds-wireguard/bin/onetun`, which `_onetun_path_or_none()` checks after
   `MNGR_ONETUN_PATH` and PATH; the resolver WARNS (naming the install command)
   when the operator key exists but the binary is missing. Non-interactive, so the
   phase-3 CI workflow can run the same step. macOS Intel has no prebuilt 0.3.10
   asset -- the installer refuses with a `cargo install` + `MNGR_ONETUN_PATH` hint.
2. **`sync-peers --via-wireguard` removed** (runbook + historical changelog entries
   annotated).
3. **The live canary restock is DONE** -- the renumbering runbook (Addendum 2b) ran
   exactly as written, with one substitution: no root was available for `wg-quick`,
   so run 1's old-identity dial used a `MNGR_ONETUN_PATH` wrapper rewriting
   `--source-peer-ip` to the old operator address instead of the kernel tunnel.
   Results: migration 034 applied + recorded on dev-josh-2; row renumbered
   `10.202.1.1 -> 10.112.1.1` (dual-written); run-2 prep completed over the real
   onetun with the new identities (first real-binary use of the argv contract --
   it matched); occupancy audit + bake all rode the userspace tunnel with public
   `:22` verified refused; the restock bake succeeded (~13 min) and the dev pool
   has one `available` gen-2 row again (`slice-7b1ef8f05baf45e289cda7b922f01885`,
   public `vps_address`, 8 units / 28G, ref `mngr/new-fleet-testing`, VM answering
   root SSH on public port 22000). The operator client config was re-rendered
   (new `/32` + new addresses) and staged at `~/.minds-dev-wg/mngr-dev.conf`
   (old config backed up as `mngr-dev.conf.pre-renumbering`).

Full observations in `gen2-turnover-notes.md` ("Operator transport (onetun) and
overlay renumbering"). With this, the stack (#571 <- #573 <- #574 <- #581 <- #609)
is merge-ready and phase 3 (CI split fleet) is next.
