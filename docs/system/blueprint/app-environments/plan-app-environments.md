# App environments: one shared environment for built-ins, isolated ones for user packages

## Overview

- **Goal: a dependency change in one app does not affect another.** Mainly user-built against built-in, and also user-built against user-built. Isolated user packages are also the starting point for bundling or publishing user apps later.
- **Built-in packages share one environment.** The libs, services and apps the template ships are members of one uv workspace, with one `uv.lock` and one root venv. Upstream reviews their dependencies together and CI tests them together.
- **No per-app uv tools.** `uv tool install` ignores `uv.lock` (it resolves fresh, and its receipt records no versions), and supervisord never ran the tool copies. Every built-in app runs from the root venv, and the update pre-flight boots the same file supervisord runs.
- **Every user-created Python package is a standalone uv project.** That covers user apps, services and libs under `system/`. Each has its own `pyproject.toml`, `uv.lock` and `.venv`; user skills stay in the shared workspace.
  - A uv workspace resolves one version per package across all members, so separate installs alone would not stop one app moving another's versions.
- **Membership is explicit.** The root `members` names every built-in package. A directory under `system/apps`, `system/services` or `system/libs` with a `pyproject.toml` that `members` does not name is a user package.
- **Every venv is a link to a directory next to the uv cache.** uv then installs by hardlink, not by copy:
  - the root venv is a 512M copy today;
  - a user app's venv would be about 88M copied, and about 1M hardlinked.
- **Existing workspaces converge through their next update.** Image and host-env changes never reach an existing workspace, so everything ships as checkout code: the boot step, the worker template, the apply, and update-self's migration script.
- **Two PRs on top of the #844 → #846 → #847 stack, all in one release:**
  - `gabriel/venvs-on-rootfs`: venv links. It carries this blueprint.
  - #920, `gabriel/single-app-install`: built-ins on the shared venv, user packages standalone, and the migration.

## Expected behavior

### Built-in apps

- Each built-in app's supervisord program execs `.venv/bin/<name>`, and the update pre-flight boots the same file.
- No app is installed as a uv tool. The first update removes app tools an older image left behind; the mngr tool stays.
- A dependency added to a built-in app changes the shared `uv.lock`, reviewed and tested upstream like any built-in change.

### User packages

- **build-app scaffolds a standalone project:**
  - its own `pyproject.toml`;
  - a `uv.lock` seeded from the root lock, so a new app starts on the built-ins' versions and resolves from the warm cache;
  - a linked `.venv`.
- **Dependencies on shared code use editable path dependencies.** A user package may depend on any workspace lib or package (`app-schema`, `workspace_layout`, a built-in app's package) this way:
  - the lib's code is shared live;
  - the lib's own workspace sources apply in the package's lock, so `imbue-common` and the mngr packages always come from the root's mngr pin;
  - the scaffold writes no `imbue-common` source of its own, because a second source for the same package fails to lock ("conflicting URLs").
- **A user program syncs and execs its own venv at start:** the venv-link helper's `ensure` for its directory, then `uv sync --locked` for its own project, then exec a script from its own `.venv/bin`, keeping wrappers such as `with_secrets` and the `oom_tag_service.py user` prefix.
  - The `ensure` matters after a root-filesystem rebuild: uv refuses a `.venv` link whose target is missing ("File exists"), so the target has to be created first.
  - A no-op sync takes about 30 ms.
  - A stale lock (after a hand-edited `pyproject.toml`) or a failed sync fails the start with a clear error, and supervisord keeps retrying.
  - `--locked` needs no network when the lock is current.
- **Program and tests share one venv.** The sync installs the default groups, `dev` included.
- **Who syncs a user package's venv:** its program at start, worktree setup, a build, or an agent running `uv sync`/`uv add` in it. The boot step creates every user package's link and target but syncs none of them, so a lib (which has no program) is synced when an agent works on it.
- **An apply that changes a user package syncs it in place.** This covers migration and relocks. Its program's sync at start handles both landing and rollback, since a rollback restores the tree and the restarted program re-syncs.
- **Adding a dependency touches only that package.** `uv add` in a user package's directory changes only its lock and venv; the root lock and the chat's environment stay as they were. Two user packages can pin incompatible versions of the same package.
- **One-off commands run a user package's tool from its project:** `uv run --no-sync --project system/<kind>/<pkg> <cli>`.
- **Shared code can still depend on a user package.** A user skill (shared) can depend on a user lib by path; that lib's dependencies then resolve in the shared lock. Nothing supports or documents opting a user package into `members`.
- **When shared libs or the mngr pin move.** If upstream changes the dependencies of a shared lib a user package uses, or moves the mngr pin, the package's lock goes stale, and the update worker relocks it.

### Where venvs live

- Each `.venv` (the root's, each user package's, each worktree's) is a symlink to `<uv cache dir>/../venvs/<key>`, and uv hardlinks its files from the cache:
  - Docker-based providers: `/var/cache/user/venvs`, on the container root filesystem next to the baked cache, so creates keep using it;
  - Lima: inside the cache's btrfs subvolume on the data disk. Lima copies today too, because btrfs cannot hardlink across subvolumes.
- **Targets on a different filesystem from the uv cache** (different device numbers, which also covers btrfs subvolumes): the helper writes a warning naming both paths to the boot log and carries on.
- **Rebuilt root filesystem.** Recreating a workspace, or restoring it onto a new container, rebuilds the root filesystem and drops the venv targets with it. The next boot, program start or worktree setup rebuilds them from the cache, with network for anything the cache lacks. Stop and start, sleep and wake, and updates keep them.
- **Real `.venv` directories get replaced.** A real directory (for example after an in-place backup restore) becomes a fresh linked venv the next time the helper runs. Offline, it stays a working real directory until a later run.
- **Each target records the link it serves.** Every run of the helper deletes targets whose link is gone or points elsewhere, at boot, at each worktree setup and in the apply. Worktrees removed outside our code (`mngr destroy`, by hand) leak nothing, and worktree cleanup needs no hook of its own.
- **No switch keeps venvs on the volume.** Every supported provider keeps root-filesystem writes across starts; mngr refuses gVisor's ephemeral-rootfs mode.
- **The apply swaps the root venv for a fresh one:**
  1. it syncs the root venv into a fresh target through its real path, so the scripts' `#!` lines name that target;
  2. it pre-flights from that target;
  3. it switches the link;
  4. it restarts once.
  - Processes started from the old venv keep using it until the restart.
  - The old target is deleted after a healthy restart, and a rollback switches the link back.
  - A later `rollback-last` re-syncs, as it does today.
  - The System Monitor's disk view counts the venv targets as system software.

### The update that ships this, on an existing workspace

- **The worker hits the standalone test.** `system/test_user_packages_are_standalone.py` (always run) fails on old-layout user packages, and its message names the migration reference.
- **The worker runs `--dry-run` first and puts the output in its report.** Then it runs the migration script on each package:
  - it seeds the package's lock from the pre-merge shared lock, so the package keeps the versions it runs today;
  - it turns `{ workspace = true }` sources into editable path sources;
  - it keeps any root-sourced package the app declares directly equal to the root pin;
  - it removes the package from the root dependencies and sources;
  - it rewrites the program line.
- **The worker fixes mechanical failures itself.** A typical one is an import the package never declared: the worker declares it at the version the shared lock had, relocks, and retries. Anything it cannot fix becomes a gate question to the lead.
- **The worker's existing step-4b checks cover each migrated package.** Each one gets its suite, and apps and services also get an isolated boot, all from the package's own venv.
- **The apply converts the root `.venv` to a link** in the same fresh-target step, before the pre-flight. The old real directory is moved aside, deleted after a healthy restart, and put back on rollback.
- **The user sees at most one plain sentence** about the restructuring, at the lead's discretion.
- **After a successful apply, broken user things get fixed, not reported.** This rule applies to every update. If anything of the user's is broken (say a user program is not RUNNING after the restart):
  - the lead says what it is fixing in one short chat line (nothing new in the app or in `run.json`);
  - it dispatches a repair worker with the evidence;
  - it finishes the run once the problem is fixed or the worker gives up. A give-up is recorded as `UPDATED`, with a short detail naming what is still broken and what was tried.

### Other flows

- **`migrate-workspace`** lands an old workspace's apps through the migration script.
- **`use-template` and `update-installed-template`** each say that an app from an older template needs converting with the script, and that resolving the adoption conflict keeps the explicit root `members`. An unconverted app fails to start, and the adopting agent, whose definition of done is a running app, fixes it. Neither skill gets an extra step, and no test asserts against a glob in `members`.
- **Workspaces created or baked from a template** that carries user packages come up with those packages' venvs already synced.
- **Worktrees:** worker setup links and syncs the root venv and every user package's venv, so test commands for standalone packages run `uv run --no-sync` in the package's directory.
- **The test gate:**
  - `select-tests` emits an import check, run in the package's own environment, for every changed Python package whose own suite has no tests;
  - the standalone test runs `uv lock --check` on every user package, about 25 ms each, with no network needed when the lock is current.

## Implementation plan

### Lower PR: `gabriel/venvs-on-rootfs` (from #847)

- **`system/scripts/workspace_bare_scripts/venv_links.py` (new, stdlib only).** It works out where targets go:
  - `venvs_root()`: `realpath` of the parent of `uv cache dir`, plus `venvs`;
  - `target_for(project_dir)`: a readable key from the absolute project path, plus a short hash.

  And it manages the links:
  - `ensure(checkout_root, project_dirs)` writes the link first and then creates the target with an owner file naming the link, so a concurrent sweep never deletes a target being set up. A real `.venv` directory is moved aside, a fresh target is synced, and the old directory is deleted; if the sync fails, the old directory goes back.
  - `sweep()` deletes each target whose owner link is missing or resolves elsewhere.
  - `fresh_target()`, `switch_link()` and `remove_target()` support the apply's swap.
  - `check_same_filesystem()` compares the device numbers of the venv root and the cache, and warns on stderr.
  - It has a CLI (`ensure`, `sweep`, `fresh`, `switch`). Subprocess calls go through an injectable runner for tests.
- **`.agents/skills/update-self/python/update_self_skill/venv_links.py`:** a byte-identical mirror, added to `system/scripts/update_self_mirrors_sync_test.py`.
- **`.mngr/settings.toml`:**
  - the bootstrap window becomes `python3 system/scripts/workspace_bare_scripts/venv_links.py ensure . ; uv sync --all-packages --frozen; exec .venv/bin/bootstrap`;
  - the `create_templates.worker` provision command runs `ensure` before its sync.
- **`system/scripts/session_start_sync.sh`:** runs `ensure` before its sync.
- **`system/scripts/build_workspace.sh` and `system/scripts/install_dependencies.sh`:** run `ensure` before their syncs, so an image build bakes the target into the image layer.
- **`system/Dockerfile`:** copies the helper into the manifest layer that `install_dependencies.sh` runs in, and its cache comment names the venv targets.
- **`system/scripts/default_workspace_template_seed.sh`:** a comment only. `cp -a` copies the `.venv` link, which resolves to the baked target at the same path.
- **update-self:**
  - `update_environment.py`: the root venv drops out of `snapshot_targets()`.
  - `update_apply.py`: the root venv step builds a fresh target with `UV_PROJECT_ENVIRONMENT=<target> uv sync --all-packages --frozen`, pre-flights from it, switches the link before the restart, deletes the previous target after a healthy restart, and switches it back on rollback. The first apply converts a real `.venv` in this same step, keeping the old directory aside until a healthy restart and putting it back on rollback.
  - `update_apply_contract.py`: the marker records the previous and new targets.
  - `update_probes.py`: the pre-flight execs the new target's `bin/system-interface` and `bin/chat-app`.
- **Docs:** this blueprint; `docs/system/workspace-internals.md` (where venvs live); `.agents/shared/references/running-python.md` (`.venv` is a link, and its target is managed).
- **Changelog entries** for every touched project.

### #920: `gabriel/single-app-install` (on the lower PR)

- **Built-ins on the root venv:**
  - program lines exec `.venv/bin/<name>`;
  - the pre-flight boots that file;
  - no per-app `uv tool install` in `build_workspace.sh`, the build-app scaffold or update-self;
  - `tool_env.py drop-app-tools` and the apply's removal of app tools, marked `CLEANUP:`;
  - env-converge's replay holds back uv tools named like apps;
  - an app-pyproject change re-syncs the venv (`ApplyPlan.app_pyproject`);
  - the Routines `[preview]` command is fixed.
- **`pyproject.toml`:**
  - `members` names every built-in lib, service and app (`system/scripts`, `.agents/shared/scripts` and `.agents/skills/*/python` stay);
  - the root dependencies list the built-in apps.
- **`system/scripts/workspace_bare_scripts/user_packages.py` (new, stdlib only), mirrored into update-self.** It provides:
  - `built_in_members(repo_root)`, from the root `members`;
  - `user_packages(repo_root)`: the `system/{apps,services,libs}/*` directories with a `pyproject.toml` that aren't members;
  - `standalone_problems(pkg)`: no own `uv.lock`, a `workspace = true` source, or a program line that doesn't sync its own project with `--locked` before execing from its own `.venv/bin` (judged by meaning, so wrappers and extra steps are allowed);
  - `lock_check(pkg)`.
- **`system/test_user_packages_are_standalone.py` (new):** runs `standalone_problems` and `uv lock --check` on every user package. Its failure message points at `python-packaging-migration.md`.
- **`system/test_app_manifests.py`:** reads the built-in list from `members`, and fails when an upstream package under `system/{apps,services,libs}` isn't listed.
- **`.agents/skills/update-self/python/update_self_skill/user_package_migration.py` (new), marked `CLEANUP:`, run as update-self's `user-package-migration` command.** It can be removed once we're reasonably confident no old templates or workspaces still need migrating. Commands:
  - `migrate <pkg> [--dry-run] [--shared-lock <pre-merge uv.lock>]`: seeds the package's lock, converts sources, keeps root-sourced direct dependencies on the root pin, removes the package from the root dependencies and sources, rewrites the program line, links and syncs the venv;
  - `relock <pkg>`: `uv lock`, which keeps existing versions, plus pin alignment.
- **`.agents/skills/update-self/references/`:**
  - `python-packaging-migration.md` gets a new "A user app, service or lib" section (symptom: the standalone test; steps: dry run, migrate, fix undeclared imports, gate otherwise, rewrite callers to `uv run --no-sync --project`). Its "A user service" section is rewritten to the standalone end state.
  - `update-self-worker.md`: the environment gate runs `uv lock --check` per user package, relocks through the command, and puts dry-run output in the report.
- **`.agents/skills/update-self/SKILL.md`:** step 5c's "carry user breakage into the results" becomes the fix-after-apply rule (one chat line, a repair worker, and `UPDATED` with a detail if it gives up).
- **build-app:**
  - `scaffold_flask_lib.py`: a standalone project (path dependency on `app-schema`, no `imbue-common` source); copies the root `uv.lock` in and runs `uv lock`; runs `ensure`, then `uv sync --locked`; writes the `ensure`-sync-exec program line; no root `uv sync --all-packages`.
  - `SKILL.md` and `references/cleanup.md`: say how standalone packages work and how to remove one.
  - `scaffold_flask_lib_test.py`: a new `acceptance` test that does a real lock and sync and boots the app.
- **Isolated boots and previews:** `.agents/shared/scripts/agents_shared/serve_isolated_instance.py` and `.agents/skills/update-app/python/update_app_skill/preview_app.py`/`preview_boot.py` boot a standalone package from its own venv.
- **`system/libs/app_manifest/src/app_manifest/selection.py` and `workspace_graph.py`:**
  - user packages are own-root units, with their commands run in the package directory;
  - an import check runs for each changed Python package whose own suite has no tests.
- **`system/test_python_launch_rules.py`:** accepts the user program line, and flags a root-venv `uv run --no-sync <cli>` that names a user package's script.
- **`.mngr/settings.toml`** (worker template) **and `build_workspace.sh`:** `ensure`, then `uv sync --locked` for every user package.
- **`.mngr/settings.toml`** (bootstrap window): its `ensure` covers every user package's link and target, without syncing them.
- **`migrate-workspace`:** `SKILL.md` and `migrate_workspace.py` land apps through the migration script.
- **`use-template` and `update-installed-template` `SKILL.md`:** one line each.
- **Agent docs:**
  - `AGENTS.md`: the `uv sync` rule covers both places (`uv sync --all-packages` at the repo root; `uv sync`/`uv add` inside a user package).
  - `.agents/shared/references/running-python.md`: user package rules.
  - `.agents/shared/references/spec-summary.md`: one line saying a skill's path dependency on a user lib pulls that lib's dependencies into the shared lock.
- **Docs:** `docs/system/blueprint/workspace-app-model/contracts.md` section 14, `plan-workspace-app-model.md` section 3.1, `system/apps/README.md`, `docs/system/workspace-internals.md`.
- **Changelog entries** for every touched project. #920's title and description are rewritten at the end.

## Implementation phases

1. **Restack #920 onto #847 (#920).** Change the base, merge `gabriel/type-check-scripts`, resolve #846's file moves, and get CI green. Behavior is unchanged.
2. **Venv links (lower PR).**
   - Branch `gabriel/venvs-on-rootfs` from #847.
   - Build the helper and its mirror.
   - Wire up the boot, session-start, worker, build and image paths.
   - Add the apply's fresh-target swap and conversion, the sweep and the warning.
   - Add the blueprint and docs.
   - Merge the branch into #920 and point #920's base at it.
   
   Result: every venv is hardlinked, and nothing else changes.
3. **User packages standalone (#920):**
   - explicit `members`;
   - `user_packages.py` and the standalone test;
   - the scaffold and its acceptance test;
   - selection (own-root units and the import check);
   - isolated boots and previews;
   - launch rules;
   - worker and build syncs;
   - agent docs.
   
   Result: new user packages are standalone. Old-layout ones fail the standalone test.
4. **Migration (#920):**
   - the migration script (dry run, `CLEANUP:`);
   - the migration reference sections;
   - the worker's environment gate and report;
   - `migrate-workspace`;
   - the template skills' lines;
   - the fix-after-apply rule and results guidance.
   
   Result: an existing workspace converges in one update.
5. **Docs and live run (#920):** the contract and plan rewrites and the apps README; the live run and its measurements; the PR descriptions rewritten.

## Testing strategy

### Unit and integration (CI)

- **`venv_links` on `tmp_path` trees:**
  - link before target, and the owner file;
  - the sweep, on valid, missing, foreign and half-created entries;
  - real-directory conversion, both success and a failed sync that restores the old directory;
  - `fresh`/`switch`/`remove`;
  - the device-number warning. A stubbed runner stands in for `uv`.
- **The apply's swap, with a fake runner:**
  - the sync uses the target's real path;
  - the pre-flight runs from the new target;
  - the link switches before the restart;
  - the previous target is deleted only after a healthy restart;
  - rollback switches back;
  - the first apply converts a real `.venv`.
- **The pre-flight and supervisord resolve to the same file,** for the shell and the chat, parsed from the real program blocks.
- **`user_packages` on fixture trees:**
  - discovery against `members`;
  - each standalone problem;
  - program-line judgments, with and without `with_secrets` and with extra steps.
- **The template test:** every upstream package under `system/{apps,services,libs}` is listed in `members`.
- **Selection:**
  - a standalone package is an own-root unit, with commands in its directory;
  - the import check is emitted only when the package's own suite has no tests.
- **Launch rules:** the new program line is accepted, and a root-venv call of a user package's script is flagged.
- **The migration script on `tmp_path` trees,** to the standard of `migrate-workspace`'s tests:
  - source conversion, root `members`/dependency/source cleanup;
  - program-line and caller rewriting;
  - pin alignment;
  - `--dry-run` output with no writes;
  - real `uv lock` on a fixture with only path dependencies (unmarked; no network).
- **The build-app scaffold** (`acceptance`): a real lock seeded from the root, `uv sync --locked`, the app booted from its own venv, and a health answer.
- **Existing suites:** update-self, `test_app_manifests.py`, `test_supervisord_layout.py`, `update_self_mirrors_sync_test.py`, and the build-app and update-app suites.

### Live run (manual, documented in the PR)

There is one combined live run at #920's tip. The venv-relocation PR has no live run of its own: its conversion and rollback steps are exercised inside the combined run. There is no read-only survey of real workspaces before the release.

**Setup:** a fresh dev slice baked at `minds-v0.9.4` (old layout), seeded with:
- a working user app, user service and user lib;
- an app that imports a package it never declared;
- an app that gets `imbue-common` only through `app-schema`.

**Steps:**
1. Run the migration dry run on the seeded workspace.
2. Start the update to #920's tip the way a user does (the lead and its worker), so the stack's migrations and ours apply together. Hand-driving the apply is only a fallback for isolating a failure.

**After the update, check:**
- every user package is standalone, on the versions it had before;
- every program is RUNNING;
- the undeclared import was declared by the worker;
- the indirect `imbue-common` still comes from the mngr pin;
- the results say at most one plain sentence;
- `uv add` in a user app of a package that would move a chat dependency leaves the root `uv.lock` and the chat's venv unchanged;
- two user apps can pin incompatible versions;
- every `.venv` is a link whose files are hardlinked from the cache.

**Then:**
- run `rollback-last`, and check the workspace comes up healthy on the previous release with linked venvs;
- adopt one old-layout catalog template that has a Python app, and check the adopting agent converts it with the script;
- on a fresh local Lima workspace at #920's tip, check that links are created, files are hardlinked into the cache subvolume, and the warning is silent.

**Measurements:**
- disk per venv, before and after;
- time to RUNNING for the seeded user programs during the update's restart;
- the apply's root venv step, before (an in-place sync behind a snapshot copy) and after (a fresh hardlinked target);
- the standalone test's lock checks across the seeded packages.

### Acceptance criteria

- A user package can add a dependency that would move one of the chat's versions, and the root `uv.lock` and the chat's environment do not change.
- Two user packages can pin incompatible versions of the same package.
- A broken user package (its dependency or its sync) stops only that package.
- An upstream update never conflicts on, or regenerates, a user package's lock.
- An existing workspace with user packages migrates through one update, and its packages keep running on the same versions.
- No venv is a full copy of what the uv cache holds.

## Open questions

- Built-in apps stay version-coupled to each other through the shared lock.
- A user's edits to a built-in app stay in the shared lock.
- Packaging and publishing user apps: editable path dependencies on workspace libs won't bundle as they are.
- Republishing the Imbue-owned catalog templates in the new layout (adoption converts them meanwhile).
- When to remove the migration script: once we're reasonably confident no old templates or workspaces need it.
- Whether agents need an `AGENTS.md` line about `ModuleNotFoundError` in user apps (add it only if agents are seen getting it wrong).
- To verify during implementation: what Imbue Studio's in-place backup restore does to a `.venv` link that backups exclude (`**/.venv`). The helper converts a real directory either way.
