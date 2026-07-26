# env_converge

Environment record + boot-time convergence for default-workspace-template
hosts: everything installed into the environment (apt packages, npm globals,
uv tools, cargo crates, pinned binaries) is captured into a record that rides
the persistent `/home/user` volume, and every boot converges the (regenerable)
rootfs back to that record at the pinned apt snapshot timestamp.

## The model

- **The base is a cache.** The Docker image / provisioned VM bakes what is
  cheap and stable; anything it lacks is converged in at boot. Restores and
  rebuilds therefore never lose installed software: a fresh rootfs plus the
  record reproduces the environment.
- **Captured state IS the manifest.** For anything with a real package
  database there is no intent file: an apt `DPkg::Post-Invoke` hook (installed
  by `scripts/setup_system.sh`) re-captures from dpkg after every apt
  operation, and boot-time probes capture `npm ls -g`, `uv tool list`, and
  `cargo install --list` + `rustup toolchain list` (rust is agent-installed,
  not in the base image, so an absent cargo captures as an empty state).
  Agents install things normally; nothing needs to be declared.
- **Cargo is a non-critical source.** Unlike npm globals (which live on the
  rootfs and exist in a restored workspace only via the record), `~/.cargo/bin`
  binaries ride the backup as real files -- so the cargo record matters for
  inspiration manifests and genuinely fresh homes, not ordinary restores. The
  replay uses `cargo install --locked <crate>@<version>` (registry crates
  only; path/git installs are not recorded) and installs the recorded rustup
  default toolchain first; when rust itself is absent, entries are reported
  `package_unavailable` rather than bootstrapping rustup.
- **Versions are a function of the snapshot timestamp.** All apt sources are
  pinned to the committed `.mngr/apt-snapshot-timestamp` (see
  `scripts/write_apt_sources.sh`), so replaying the recorded *names* yields
  the recorded *versions*. Versions change only at the explicit
  `env-converge upgrade` (run by the update-self flow), never on restore,
  restart, or re-converge.
- **Declared units are code.** `scripts/env.d/<NNNN>-<name>.sh` are plain,
  dumb bash scripts run in lexical order by the slow phase: each must be
  idempotent with a fast satisfied-check (exit 0 in milliseconds when nothing
  to do). There are NO marker files -- skip-speed comes from the check, and
  version stability comes from the pins inside each unit. Units receive
  `ENV_CONVERGE_WORKSPACE_DIR` and `ENV_CONVERGE_OVERLAY_DIR` and run with the
  pinned apt sources already in place. A failing unit is isolated (logged +
  evented, never blocks the others or boot).
- **The overlay convention** persists rootfs paths that services need durable:
  each absolute path listed in `scripts/env.d/overlay-paths.json` is symlinked
  to `/home/user/overlay/<abs_path>` by the fast phase (run synchronously by
  bootstrap BEFORE supervisord, so services never write to a doomed rootfs
  path). First application adopts pre-existing rootfs content into the
  overlay; when both exist, the overlay (user data) wins. Everything else
  written outside `/home/user` -- hand-edits to /etc, stray rootfs files --
  is NOT captured and dies with the container.

## Phases and ordering

- **Fast phase** (bootstrap, pre-services): overlay symlinks only. Instant,
  no network.
- **Slow phase** (the `env-converge` supervisord one-shot; never blocks
  boot): run all env.d units, install record entries missing from this
  rootfs, re-capture, stamp.
- **Removal stickiness**: on a rootfs carrying the identity stamp
  (`/var/lib/minds/env-converge/rootfs-id`), capture runs before the replay so
  deliberate uninstalls stick; on a fresh rootfs (rebuild / restore) the
  record wins, then capture + stamp.

## On-disk shape

- Record: `$MNGR_HOST_DIR/plugin/env-converge/{base,apt,npm,uv,cargo}.json`,
  atomically rewritten, jq-friendly.
- Events: `$MNGR_HOST_DIR/plugin/env-converge/events/env_converge/events.jsonl`
  (captures, unit runs, package_installed / package_unavailable, upgrades).

## CLI

- `uv run env-converge run [--phase fast|slow|all]` -- converge (exit 3 when
  some recorded packages were unavailable).
- `uv run env-converge capture` -- re-capture actual state into the record.
- `uv run env-converge upgrade` -- advance to the repo's committed snapshot
  timestamp: re-render sources, `apt-get full-upgrade`, re-run units,
  re-capture, and print the version deltas. Bundled into the update-self flow.
- `uv run env-converge status` -- record vs reality summary as JSON
  (including whether an upgrade is pending).

The minds in-place backup restore restarts this program
(`supervisorctl restart env-converge`) after rewinding `/home/user`, which is
what makes a restored record converge the rootfs back to exactly the restored
package set.
