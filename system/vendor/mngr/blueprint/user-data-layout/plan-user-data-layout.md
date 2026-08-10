# Plan: user-data-layout

Move minds workspaces to a sane user-data layout (`/home/user` as the single persistent tree), pin the entire apt universe to per-environment snapshot timestamps served from our own mirror, and capture/converge the full environment (apt + npm + uv tools + binaries) so backups contain exactly user data and restores reproduce exact package versions.

Three sequential PR trains: (1) trixie + snapshot pinning + mirror, (2) layout cutover, (3) environment record + converger. Hard cutover: no migration of existing workspaces or backups; existing workspaces keep working untouched until destroyed.

## Overview

- **One persistent tree**: the per-host volume (btrfs subvolume where available) mounts at `/home/user`; backup unit = volume = user data by construction. `usermod -d /home/user root` (stay root). host_dir becomes `~/.mngr`, work_dir `~/workspace`, worktrees `~/worktrees`.
- **mngr core must not break for outside users**: all changes are additive, default-preserving config (`host_volume_mount_path`-style knob to decouple volume mount from host_dir; `host_log_dir` knob for plain-text service logs). dwt/minds opt in via settings.
- **Separate user data from regenerables**: caches redirected off-volume (`XDG_CACHE_HOME=/var/cache/user` + `~/.cache` symlink, `npm_config_cache`); `/tmp` and `/run` tmpfs; plain-text logs to `/var/log/mngr` and `/var/log/supervisor` (not backed up); structured `events/` stay in host_dir (backed up). Restic excludes only `.venv`, `node_modules`, and `~/.ssh/authorized_keys` (provisioning-owned).
- **Deterministic dependencies via captured state, not intent manifests**: apt `DPkg::Post-Invoke` hook + `npm -g`/`uv tool` probes write per-source JSON records under `~/.mngr/plugin/env-converge/`; curl'd binaries are pinned script units. All apt operations resolve against snapshot sources pinned to a single per-environment timestamp `T`; versions change only at explicit upgrade (bundled into update-self), never on restore/restart.
- **Self-hosted apt snapshot mirror**: a small stateless Cloudflare Worker route in `remote_service_connector` maps `/snap/<T>/` onto frozen index sets + a shared lazily-warmed R2 pool (read-through to deb.debian.org, snapshot.debian.org for superseded files; full warm after each cut). Upstream Debian signatures preserved (no key custody). One production mirror serves all tiers; every cut `T` kept forever.
- **Boot-time convergence**: a two-phase `env-converge` (fast: overlay symlinks + record load, synchronous in bootstrap; slow: package installs, supervisord one-shot) absorbs deferred-install and install_secret_scanners as `scripts/env.d/<NNNN>-<name>.sh` units. No markers: units are idempotent with fast satisfied-checks; re-run every boot.
- **Deferred to later PRs** (explicit non-goals here): workspace-root declutter, inspiration-manifest restructuring (a `specs/` note is committed with this work), Claude config relocation, fresh-workspace recovery flow, fleet-migration UI, Python 3.13.

## Expected behavior

### After train 1 (trixie + pinning + mirror)

- New workspaces run Debian trixie (Python stays 3.12) on every provider: docker image base `python:3.12-slim-trixie`, Lima guests boot a pinned trixie genericcloud image (via dwt provider config; mngr_lima default unchanged), Modal/lima provisioning runs the same scripts. VPS/pool outer VMs move to trixie where we control the image (no pinning machinery on outers).
- All apt sources inside a workspace point at the mirror at the committed `T` (with a snapshot.debian.org same-`T` lower-priority fallback), so `apt-get install` always resolves the same versions for a given `T`; mirror outage degrades to slow, not broken.
- No third-party apt repos remain: nodejs comes from trixie main; `gh` installs as a sha-pinned GitHub-release binary.
- Security updates arrive only when `T` advances (accepted posture; container/gVisor isolation is the boundary).
- Cutting a release: the release-minds runbook calls the connector admin route to cut the new `T` (freeze indexes, minutes) before landing the `T`-bump commit; async full-pool warm follows; a missing package can never be "in the index but not fetchable".
- Existing workspaces are untouched (they keep bookworm and live sources until destroyed).

### After train 2 (layout cutover)

- A new workspace's shell lands in `/home/user/workspace`; `ls ~` shows `workspace/`, `worktrees/`, `overlay/`, plus dotfiles; mngr state is hidden at `~/.mngr` (agents, events, env file, shared Claude config); no `/mngr`, `/code`, or `/worktree` paths exist (no compat symlinks).
- `$HOME` is `/home/user` for root everywhere (passwd entry updated), so sshd, `Path.home()`, and dotfiles all agree.
- Backups contain exactly `/home/user` (code, worktrees, agent state, transcripts, events, uploads, dotfiles, overlay data) minus `.venv`/`node_modules`/`authorized_keys`; caches and logs never appear in backups; `~/.mngr/layout-version` = `2` stamps every backup self-describingly.
- Plain-text mngr service logs write to `/var/log/mngr/`; supervisor logs stay in `/var/log/supervisor/`; `events/` remain under `~/.mngr` and are backed up.
- `~/.ssh/authorized_keys` is rewritten by mngr provisioning on start/restore, so restoring a backup can never lock the tooling out.
- btrfs snapshot/backup flows (lima direct, vps outer-trigger) work unchanged against the new mount; provider-internal paths (`/mngr-vol`, `/mngr-btrfs`, `/mngr-snapshot(s)`) keep their names.
- Old-layout backups are not restorable into new-layout workspaces (hard cutover; manual `restic restore` to a scratch dir remains possible).

### After train 3 (env record + converger)

- Every apt operation updates `~/.mngr/plugin/env-converge/apt.json` automatically (Post-Invoke hook); boot probes capture `npm -g` and `uv tool` state; `base.json` records template ref, image identity, arch, and `T`.
- On every boot: bootstrap applies `overlay-paths.json` (adopt-and-move semantics) before services start; the `env-converge` supervisord one-shot re-runs all `env.d` units (idempotent, fast satisfied-checks) and installs anything in the record that is missing from the rootfs, at the recorded `T`. Failures never block boot; they emit `package_unavailable`/`tick`-style events.
- Removals stick: on a known rootfs (identity stamp present) capture runs before converge; on a fresh rootfs (rebuild/restore) converge runs first from the record, then captures and stamps.
- In-place backup restore rewinds `/home/user`, then `supervisorctl restart env-converge` brings installed packages back to exactly what the restored record says (same `T` → same versions).
- `update-self` bundles the upgrade: advance `T` to the repo's committed value, rewrite sources, `apt full-upgrade`, re-run units, re-capture, event the deltas — one coherent "you're now current" operation.
- Anything a service needs persisted outside home is symlinked via the overlay convention (`<abs>` → `~/overlay/<abs>`); all other rootfs drift (e.g. hand-edits to `/etc`) is documented as lost on rebuild.
- `uv run env-converge status` reports record vs reality for humans and scripts (JSON).

## Implementation plan

### Train 1 — trixie, snapshot pinning, mirror

**apps/remote_service_connector (mirror):**
- New worker route (public, unauthenticated, bound to a stable custom domain): `GET /snap/<T>/dists/...` serves frozen index objects from R2; `GET /snap/<T>/pool/...` serves the shared pool cache with read-through (deb.debian.org, then snapshot.debian.org at `T` for vanished files), permanent caching, upstream 5xx retry/backoff, brief negative-caching of 404s.
- New admin routes (existing `MINDS_ADMIN_KEY` auth pattern): `cut` (fetch + freeze the full dists/ index set for a `T` into R2, synchronous, idempotent) and `warm` (async walk of the `T` index closure fetching every pool file through the read-through path; re-runnable completeness check reporting missing counts).
- R2 bucket layout: `snap/<T>/dists/...` per cut; `pool/...` shared, content-keyed by upstream path. Keep-forever retention.
- Data types (pydantic, per repo style): cut/warm request/status models; structured logs for miss/fetch telemetry (the fleet-visibility channel).

**default-workspace-template:**
- `.mngr/apt-snapshot-timestamp`: one-line committed `T` (snapshot.debian.org format `YYYYMMDDTHHMMSSZ`).
- New `scripts/write_apt_sources.sh`: renders `/etc/apt/sources.list.d/` from `T` + mirror URL (mirror primary, snapshot.debian.org same-`T` fallback via apt priorities, `check-valid-until=no` where needed). Called by the Dockerfile and by setup_system.sh (covers lima/modal/VPS-rebuild paths).
- `Dockerfile`: base → `python:3.12-slim-trixie`; run `write_apt_sources.sh` first; drop NodeSource block from setup flow.
- `scripts/setup_system.sh`: call `write_apt_sources.sh`; install nodejs from trixie main (drop NodeSource); install `gh` as a sha-pinned GitHub-release binary (drop the gh apt repo); revalidate/bump every pinned tool for trixie (restic sha256s unchanged, ttyd, cloudflared, uv, claude, earlyoom config, supervisor unit masking still applies).
- `.mngr/settings.toml`: lima provider block gains pinned trixie genericcloud `default_image_url_*` overrides; revalidate the M5/SVE `OPENSSL_armcap` workaround comment against trixie guests.
- Fallout inventory (verify, fix as needed): runsc/gVisor on trixie, fortress/Chromium apt deps under `playwright install --with-deps`, secret scanners, frontend build, `_provision_guard` interactions.

**mngr monorepo (train 1):**
- `libs/mngr_vps` / `libs/mngr_ovh` / `libs/mngr_vultr` / aws provisioning: select trixie outer images where we control the choice (no pinning/capture on outers).
- `apps/minds/docs/release.md` (+ release-minds skill): add the cut-then-bump step and the initial-`T` bring-up runbook (deploy route → operator cuts initial `T` → dwt pins sources).

### Train 2 — layout cutover

**mngr monorepo (additive, defaults unchanged):**
- `libs/mngr/imbue/mngr/providers/docker/config.py` + `instance.py`/`volume.py`: new `host_volume_mount_path: Path | None` (default None = today's mount-at-host_dir); when set, mount the per-host volume (or subpath) there and create `host_dir` inside it.
- New `host_log_dir` config (provider/host level, default `host_dir/logs`): thread through `ssh_host_setup.py`, `resources/activity_watcher.sh`, shutdown/idle scripts, `offline_host.py`/`gc.py` readers.
- `libs/mngr_vps/container_setup.py` + `instance.py` + `host_store.py`: volume layout gains a `home/` subdir beside `host_state.json`/`agents/`/`host_dir/`; when the new config is set, bind `home/` at `/home/user` and point container host_dir at `/home/user/.mngr`. `/mngr-vol` name and snapshot mounts unchanged.
- `libs/mngr_lima/lima_yaml.py` + `config.py`: configurable data-disk symlink target (default `host_dir` as today; dwt sets `/home/user`).
- Sweep for residual `/mngr` assumptions in mngr core (known: docker backend default — keep; Modal symlink comment).

**default-workspace-template:**
- `Dockerfile`: `usermod -d /home/user root`; `WORKDIR /home/user/workspace`; drop `/code` + `/worktree` symlinks; home-skeleton creation moves to seed.
- `scripts/default_workspace_template_seed.sh`: seed `~/workspace` from `/docker_build_code`; create skeleton (`~/.mngr`, `~/worktrees`, `~/overlay`, `~/.cache → /var/cache/user`, `/var/cache/user`); stamp `~/.mngr/layout-version` = `2`.
- `.mngr/settings.toml`: `target_path=/home/user/workspace/`, `worktree_base_folder=/home/user/worktrees/`, `TICKETS_DIR`/`OOM_PRIORITY_RUNTIME_DIR` under the new work_dir, `XDG_CACHE_HOME` + `npm_config_cache` host_env, tmux conf source path, `--tmpfs /tmp` start args, mngr `host_volume_mount_path`/`host_log_dir` settings per provider template.
- `supervisord.conf`: `directory=/home/user/workspace`; all program paths.
- `libs/bootstrap`, `libs/host_backup` (capabilities host_dir/`findmnt` target, excludes += `authorized_keys`, backup source = `/home/user`), `libs/github_sync`, `scripts/*`, skills, `CLAUDE.md`/docs: rename paths in place (no shared-constants layer).
- `scripts/setup_system.sh`: `.bashrc`/profile references (`~/.mngr/env` sourcing), known_hosts under the new home.

**apps/minds:**
- `backup_workspace_scripts.py`, `backup_update.py`, `backup_status`/provisioning modules, `agent_creator.py`, deployment_tests: new paths; restore scripts' host_dir argv values.
- Verify authorized_keys re-provisioning covers the post-restore path.

### Train 3 — env record + converger

**default-workspace-template — new `libs/env_converge`:**
- `data_types.py`: record models (BaseIdentity, AptState, NpmState, UvToolState, BinariesState), converge/upgrade results, event envelopes.
- `record.py`: atomic per-source JSON read/write at `~/.mngr/plugin/env-converge/` (`base.json`, `apt.json`, `npm.json`, `uv.json`, `binaries.json`); rootfs identity stamp handling.
- `capture.py`: dpkg dump parsing (hook payload), `npm ls -g --json` / `uv tool list` probes.
- `converge.py`: fast phase (apply `overlay-paths.json` with adopt-and-move; instant, no network) and slow phase (run `env.d` units in lexical order with failure isolation; install record-vs-rootfs missing apt/npm/uv entries at recorded `T`); capture-first vs converge-first ordering by stamp.
- `upgrade.py`: advance `T` to the repo's committed value, rewrite sources, `apt full-upgrade`, re-run units, re-capture, event deltas.
- `cli.py`: `env-converge run [--phase fast|slow] | capture | upgrade | status`.
- `events.py`: `events/env_converge/events.jsonl` (captures, unit runs, `package_unavailable`, upgrades, restores).
- apt hook: `/etc/apt/apt.conf.d/90env-converge-capture` + tiny hook script (installed by setup_system.sh; no-ops outside a booted workspace so image builds don't capture).
- `scripts/env.d/<NNNN>-<name>.sh` units (dumb bash, documented env `ENV_CONVERGE_OVERLAY_DIR` etc., idempotent + fast satisfied-check): split from `deferred_install.sh` (playwright/fortress) and `install_secret_scanners.sh`; delete the old marker logic and `/var/lib/minds/deferred-install`.
- `scripts/env.d/overlay-paths.json`: initial (likely near-empty) overlay list.
- `supervisord.conf`: `[program:env-converge]` one-shot replaces `[program:deferred-install]`; `libs/bootstrap`: fast-phase call before supervisord exec; first-boot baseline capture.
- `update-self` skill: bundle `env-converge upgrade` into the flow; docs: the rootfs-drift boundary + overlay convention in `CLAUDE.md`/README.
- New `specs/` note: future inspiration-manifest restructuring (declared per-inspiration packages/permissions, structured manifest, services.d), referencing this design.

**apps/minds:**
- Restore script: add `supervisorctl restart env-converge` after service restart; surface converge outcome in the restore result.

## Implementation phases

1. **Mirror online** (connector): worker route + admin cut/warm + R2 bucket + custom domain; deploy; operator cuts initial `T`. System works: mirror serves a pinned trixie universe; nothing consumes it yet.
2. **Trixie everywhere** (dwt + mngr provider images + release docs): pinned sources, base-image bump, third-party repo elimination, fallout fixes. System works: new workspaces are trixie with deterministic apt at `T`; old layout unchanged.
3. **mngr knobs** (mngr core + vps/lima plugins): `host_volume_mount_path`, `host_log_dir`, vps `home/` subdir, lima symlink target — all default-off with tests. System works: mngr unchanged for everyone; knobs proven by unit/integration tests.
4. **Layout cutover** (dwt + minds): flip the knobs in dwt settings, usermod, new paths everywhere, backup/restore path updates, layout-version stamp. System works: new workspaces on the new layout end to end (create, backup, in-place restore, all providers).
5. **Record + converger** (dwt + minds): env_converge lib, hook, probes, env.d migration, bootstrap fast phase, update-self upgrade, restore hook-in, specs note. System works: full capture/converge/upgrade/restore-exact behavior.

Each phase lands as its own PR train with changelog entries in every touched project; dwt changes ride the vendor-sync/release process between trains.

## Testing strategy

- **Train 1**: existing suites (dwt CI, mngr provider tests) + manual minds-dev-workflow verification on docker + lima + one vps release-test run. Connector: unit tests for cut/warm/read-through models per connector conventions; manual cut + warm + `apt-get update`/`install` against the mirror from a scratch trixie container. Edge cases: cold pool fetch of a superseded package (snapshot.debian.org fallback), 5xx backoff, re-cut idempotency.
- **Train 2 (mngr knobs)**: unit tests for config defaults/round-trips; docker/vps/lima integration tests exercising the knob-on path (volume at `/home/user`, host_dir inside, logs at `host_log_dir`); explicit tests that knob-off behavior is byte-identical to today.
- **Train 2 (cutover)**: update existing tests to new paths; manual minds-dev-workflow on docker + lima; one pre-merge vps/pool release-test run; manual backup → in-place restore on the new layout; verify authorized_keys is excluded and re-provisioned; verify nothing outside `/home/user` persists across container rebuild.
- **Train 3**: unit tests (record round-trips, dpkg/npm/uv parsers, overlay adopt-and-move, ordering rule by stamp, unit enumeration/isolation, sources rendering, upgrade delta computation); integration tests against a scratch prefix (converge installs a missing recorded package; removal-stickiness both orderings; restore-then-converge). Manual: full converge/restore verification on docker per Q22. `env.d` unit satisfied-checks exercised by running converge twice (second run near-instant).
- Ratchets/inline-snapshot updates where counts shift; changelog entries per project per PR; `just test-offload` green before each merge.

## Open questions

- Mirror domain name and Cloudflare zone/ops ownership (agreed: stable imbue-owned custom domain; exact name is an ops choice at implementation time).
- Sequencing assumption: no significant in-flight work collides with these surfaces (dwt Dockerfile/settings, vps/lima providers, host_backup, minds backup scripts, connector) — unconfirmed (Q50 unanswered).
- Rollout gating: plan assumes the normal staging → production release process with no extra soak requirements (Q51 unanswered; add soak steps if desired).
- Whether mngr_lima's default guest image bumps to trixie later as routine maintenance (out of scope here; dwt overrides regardless).
- Exact `T` bring-up timing for train 2/3 development branches (dev builds need a cut `T`; covered by the initial-cut runbook, but worth confirming the first cut happens before dwt trixie work starts).
- Warm completeness reporting: what "complete" means when upstream removes files mid-warm (expected resolution: completeness check counts misses resolved via snapshot.debian.org; purely informational).
