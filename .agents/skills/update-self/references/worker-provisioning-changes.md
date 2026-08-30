# Worker: provisioning changes and global-dependency bumps

A change to `system/scripts/setup_system.sh`, an installer it chains
(`install_*.sh`, `write_apt_sources.sh`, `_provision_guard.sh`), or `.mngr/**`
has no *running* consumer to grep for -- nothing imports it -- yet it installs
and configures the global toolchain (the latchkey CLI, uv, claude, modal, the
secret scanners) and the `mngr create` config every live agent, service and
future sub-agent runs on. Never conclude "nothing to apply" for one. Work each
change through, most-live-applicable first, and record what you found in your
report. You stay in your worktree: you make the in-repo edits an apply
implies; the apply script runs the provisioner and the restart.

## Toolchain-script pins

A pinned-version bump in `setup_system.sh` or an installer it chains (e.g.
`LATCHKEY_VERSION`) is **live-applicable**: the apply re-runs the idempotent
provisioner before the restart, installing the new version. A hunk only a
fresh image build reproduces is **rebuild-only**.

## `.mngr/**` settings

`.mngr/settings.toml` only governs `mngr create`, so the merged file governs
every *future* create automatically (a new workspace, the sub-agents
`launch-task` spawns). But the *current* workspace was built and launched under
the **old** settings, so a create-time change does not reach it on its own.
Examine each changed setting and best-effort make it live. Lean hard toward
applying live: most settings have a live counterpart, and "it's fiddly" is not
a reason to defer -- only a genuine lack of any live lever is.

**Ground every apply in how `system/vendor/mngr` consumes the setting.** For
each changed key, grep `system/vendor/mngr` for its name to find exactly where
mngr reads and enacts it at create time, then mirror *that* mechanism (a
`commands.create` `host_env__extend` change: find where mngr turns those
entries into the agent container's environment, and set them live in the same
place; `settings_overrides` -> where mngr writes Claude's settings;
`extra_provision_command` -> how and when mngr runs it; `disable_plugin` ->
where the plugin list is applied). Applying the setting the way mngr itself
does is what makes the live edit correct rather than a plausible guess.

Cases, most clearly applyable first:

- **Env vars and agent behavior** (`host_env` / `pass_env` / `pass_host_env` /
  `env`, `settings_overrides` like `model` / `fastMode`, `disable_plugin`) are
  **live-applicable**: they shape the environment and config each process
  reads *at launch*, so mirror the change into the live equivalent (an env var
  into a `profile.d` entry or the relevant supervisord program's
  `environment=`; an agent-behavior override into whatever the running agent
  reads); the apply's restart then picks it up. Do the mirror edit in your
  branch so it merges and is validated.
- **A toolchain/version pin under `[agent_types.*]`** (the Claude version) ->
  mirror into the `setup_system.sh` default so the provisioner re-run installs
  it. Keep lockstep pins (`agent_types.claude.version` vs the
  `CLAUDE_CODE_VERSION` default and the installed binary) consistent across
  every file that carries them. An `extra_provision_command` addition -> the
  lead runs that command live.
- Only a **container build/launch parameter** an already-running container
  genuinely cannot adopt -- a `[create_templates.*]` / `[providers.*]`
  `build_arg`, a `start_arg` (`--security-opt`, `--tmpfs`, `--workdir`,
  `--cpus` / `--memory` / `--disk`, `--restart`), or a runtime/provider flag
  (`runsc` / `docker_runtime` / `install_gvisor_runtime`) -- is
  **rebuild-only for the current workspace** (it still governs future
  creates). Flag it as needing a workspace recreate, like an image-level
  `Dockerfile` hunk; do not imply it is in effect.

**Escape hatch (`stuck`).** If a provisioning change is not live-applicable
**and** leaving the running workspace on the old provisioning would
**genuinely break it** (not merely "won't take effect until the next create"),
report `stuck` (Step 6), name the setting and why it breaks, and refuse the
update so the live workspace is left untouched. A change that is simply
deferred-until-rebuild is `done` plus a rebuild flag.

## A global-dependency bump with a dependent

When a merge bumps a *global* dependency (a `setup_system.sh` or installer
pin, or a `Dockerfile` toolchain pin), whether it is safe to apply live
depends on **who consumes the new version**. Your worktree cannot validate the
pair -- worktree isolation isolates the repo tree, not the host-global
toolchain, so your env still has the old dep; do **not** globally install the
new one to test, that mutates the toolchain the live workspace and other
agents run on. Decide by the **provenance** of the dependent (origin, not
directory: `git cat-file -e "$TARGET_REF":<path>` for its files).

- **Dependent is built-in code** (present in upstream at the target ref):
  **live-applicable** -- upstream tested that code against the bumped
  dependency together, the same "trust upstream's testing" basis the whole
  pulled-in set rides on. You do not run the bump yourself and do not
  re-validate the built-in; judge it safe and say so.
- **Dependent is user-created** (absent from upstream): **unsafe to
  hot-apply**. Upstream never tested that code against the new dependency and
  you cannot either. Classify it **rebuild-only** -- the safe landing is a
  workspace recreate, which provisions the new substrate and re-runs the user
  code against it. If leaving it unapplied would break the running workspace,
  that is `stuck`.

For either case, **research the version change online** -- the dependency's
release notes for the exact old -> new delta (breaking changes, removed flags,
new minimum runtimes); do not rely on memory -- and **report the coupling**
explicitly: which dependent, built-in or user-created, what you could and
could not validate, and your apply / rebuild-only / `stuck` call.
