# Plan: scripts as packages, launch discipline, and type-checking everything

> **Make every Python script in the template a real package on one interpreter story, run long-lived processes without uv, run one-offs with `uv run --no-sync`, and type-check all of it -- including users' own skills -- so the `sys.path` workarounds go away and startup cost becomes a deliberate choice.**

<details>
<summary>Refined prompt (the decisions this plan implements)</summary>

> read through agent ca6df7ba-2cb0-434f-9dc0-764a288d8661 thoroughly. let's spec this refactor out.
> * Existing users' script-bearing skills are migrated by update-self's merge worker when the update's environment gate (`uv sync`) and root checks fail, with the changelog naming this release as the refactor; no scripted migration.
> * Each skill with scripts makes `scripts/` its own uv project (`scripts/pyproject.toml`, package in `scripts/<pkg>/`), discovered by a `.agents/skills/*/scripts` members glob.
> * A bash-only `scripts/` dir gets a minimal `pyproject.toml` (`[tool.uv] package = false`); the meta ratchet requires a `pyproject.toml` in every skill `scripts/` dir, and the skill instructions say so.
> * Skill package naming is `<skill_name>_skill` (uv project `<skill-name>-skill`).
> * Venv-tier skill scripts are invoked as `uv run --no-sync .agents/skills/<name>/scripts/<entry>.py`, the entry file being a thin dispatcher into `<name>_skill`; existing paths (incl. minds_evals') keep working.
> * `validate_skill.py` requires `scripts/pyproject.toml` with the right project/package names, runs `uv lock --check`, and runs every entry with `uv run --no-sync <entry> --help`.
> * `.agents/shared/scripts` becomes its own venv-tier uv project (package `agents_shared`), listed explicitly in the root members, with callers on `uv run --no-sync`.
> * Skill dependencies must co-resolve with the workspace lock (same rule as apps); a clash surfaces at `uv lock` time, with no isolated-environment escape hatch.
> * Bare tier (system `python3`, stdlib only): guard hooks, bug-report collector, pre-sync build scripts, minds/minds_evals-facing scripts, `oom_priority/bin` wrappers, update-self, `provision_backups`, `require_create_account`, `run_in_background`, and `set_mngr_pin`.
> * A script is bare only if it must run without the venv, OR something outside this release invokes it by path, OR it sits in a long-running process's launch chain (so `with_secrets` and `forward_port` are bare); everything else is venv tier, so `refresh_workspace_view` moves there and update-app's docs change with it.
> * `system/scripts` becomes one uv project with two packages: a bare package holding exactly the scripts that meet the tier rule, and a venv-tier package for everything else (including `docs_viewer`, `migrate_claude_auth`, `smoketest_app`, `refresh_workspace_view`, `check_changelog_entries`).
> * Bare entry points stay as stub files at today's paths; a stub imports its sibling package with no path edit, and `sys.path` edits survive only in stubs needing libraries outside `scripts/`.
> * A permanent test invokes each externally-called stub path under `python3 -S -s` with `--help` or a harmless subcommand and requires exit 0.
> * Type checking is one root `[tool.ty]` checked against the root venv (covering `.agents`, `system/scripts`, root-level `system/` tests, `conftest.py`) plus a second ty check of the bare tier against an environment with no third-party packages, replacing the static stdlib-only AST test.
> * The Python floor is 3.12 everywhere: `requires-python >=3.12` for the root and the 16 projects at `>=3.11`; the bare ty check targets 3.12; the 3.12 install workaround in `setup_system.sh`/`build_workspace.sh` is removed once verified by a fresh Lima build; pre-2026-09-14 local Lima workspaces (Debian 12, system Python 3.11) are not supported for updates. This all lands in PR 3, together with the guard below.
> * The bare `update_self.py` stub checks the interpreter version before importing its package. On anything older than 3.12 it exits with a distinct code and a message saying the update is impossible on this workspace and pointing to `migrate-workspace`. Update-self's Step 3a (the first staged-copy run) tells the lead to stop on that exit, report the update as impossible, and offer `migrate-workspace`. A test runs the stub with a faked older version and asserts the exit code and message.
> * Launch principles (from 23f0b3f3's uv overhead analysis): long-running processes run directly without uv; one-offs may use `uv run --no-sync`, with imports optimized so startup overhead is small. On gVisor, `--no-sync` leaves ~40 ms of uv while a pydantic CLI's imports cost ~450-560 ms, so startup imports are the main lever. #759 (merged 2026-10-08) made the `workspace-layout` CLI a stdlib-only thin client.
> * Three stacked PRs on #831: (1) launch discipline (long-running processes run directly; agent-facing plain `uv run` becomes `--no-sync`, excluding skill/system script invocations), (2) scripts become packages with their invocations on `uv run --no-sync`, (3) type checking.
> * The PR split exists only for testing and review: all three are expected to merge together or close together, so user workspaces migrate once and the one migration reference covers everything.
> * A paired mngr PR on `gabriel/skill-scripts-as-packages` (matching PR 2's template branch name) converts minds_evals' and minds_admin's `uv run` invocations of template scripts to `--no-sync` and runs their tests against PR 2's branch.
> * Every long-running program line uses `bash -c "uv sync --all-packages --frozen && exec .venv/bin/<name>"`: host-backup, share-gateway, env-converge (unchanged), plus the `bootstrap` window, github-sync, and the user-service guide and its example.
> * The `bootstrap` window uses `;` instead of `&&`: a failed boot-time sync (e.g. a skill `scripts/` dir with no `pyproject.toml`, which fails every uv command in the workspace) still starts supervisord on the existing venv, so the workspace stays reachable for an agent to repair it.
> * The `SessionStart` sync checks the lock. If it's stale, it relocks and prints a message that the lock was regenerated and should go into the agent's next commit, phrased so the agent doesn't act on it before the user's first message. Then it syncs `--frozen`.
> * A root test fails on any supervisord program line that runs under `uv run`, so the update worker fixes existing user services the same way it migrates user skills.
> * On the scratch workspace after PR 1: `ps` shows no `uv` process parenting supervisord or any program, every program reaches RUNNING, and the PR records per-process memory before/after.
> * All agent-facing plain `uv run` (dev tools included) becomes `--no-sync`. A root check scans tracked `.md`, `.toml`, `.sh` and `.conf` files for plain `uv run`, skipping changelog dirs, consolidated changelogs and test fixtures; any remaining deliberate use is listed with file and context in the check.
> * In PR 1, the secrets guard and any other hook that parses `uv run` learn to skip a short allowlist of harmless options (`--no-sync`, `--frozen`, `--quiet`); any other option after `uv run` stays unrecognised and blocked, with tests for both.
> * Each venv-tier entry point has a test that it doesn't load heavy modules (pydantic, loguru, click, tenacity, httpx) at import, apart from ones it declares in its skill's `scripts/pyproject.toml` under a template-defined table. Heavy imports are lazy and must not stop type checking from working; the aim is to be deliberate about import cost.
> * Bare entry points, guard hooks included, get the same kind of test against a short list of expensive stdlib modules. PR 2's timing table includes each hook entry point before/after, and a regression is checked by hand in the PR, not in CI.
> * Consolidating the guard hooks into one process, and the rewrite hook's `Bash` matcher, are out of this stack: MIND-485 (Todo), on main after the stack merges.
> * The skill-authoring docs (`type-skill.md`, `spec-summary.md`) set the rule for every venv-tier entry point: the entry module imports only light modules at top level, subcommands import their implementation lazily, and no `TYPE_CHECKING` is needed.
> * The inline-import ratchet in mngr's `imbue_common` stays as is. Library CLIs under `system/` follow #759's thin-client approach (stdlib only at the entry point); lazy imports are allowed only in skill and script packages, which that ratchet doesn't cover.
> * The launch and packaging principles live in one new reference, `.agents/shared/references/running-python.md`, which the existing docs link to.
> * The root `AGENTS.md` gets a one-line rule (one-offs use `uv run --no-sync`; never run a long-running process under `uv run`) linking to `running-python.md`.
> * An agent never needs to know a script's tier: the rule (stated in `running-python.md` and the `AGENTS.md` line) is to run any script with `uv run --no-sync` unless the instruction being followed says `python3`. A bare script also runs fine under uv, while `python3` on a venv-tier script fails loudly with `ModuleNotFoundError`.
> * A root check in PR 2 requires every `python3 <path>.py` in a tracked doc, prompt, config or script to name a bare entry point, so a missed rewrite of a script that moved to the venv tier is caught.
> * The update worker gets the changelog entries plus one general migration reference inside the update-self skill, covering all of these Python-running changes (user skills, user services, `uv run` invocations). It has no mechanism for choosing which notes apply; MIND-486 (Backlog) is the later migration-doc system that will reshape it.
> * Before/after timings of each changed entry point's `--help` (median of 15) are taken on the scratch workspace (before at the latest release, after once updated to the stack); workspace-1 is left alone.
> * PR 2's manual checks include a real update-self run on a dev-tier imbue-cloud scratch workspace, started from the latest `minds-v*` release tag, with a planted user skill (PEP 723 scripts, plain `uv run` invocations) and a user service under `uv run`. It passes when the worker migrates both, everything starts, and the checks pass.

</details>

## Overview

- **Why.** Skill scripts and `system/scripts` were written as standalone files on the theory that each is an isolated unit. In practice that theory is worked around everywhere: 11 files with `sys.path` inserts (plus 6 in `oom_priority/bin`), about 33 test files loading scripts by path, 8 PEP 723 scripts with private environments, and nothing type-checks any of it (114 ty errors at #831's tip `1f5697934`, after its main merge). Packaging the scripts removes the workarounds and makes them checkable.
- **The real constraint is the environment, not the layout.** Some code must run when the venv is missing or broken, or is called by path from another release, or sits in a long-running process's launch chain. That is the **bare tier**: system `python3`, stdlib only. Everything else is the **venv tier**. Both are packages; only the interpreter differs.
- **Startup cost: imports matter more than uv.** 23f0b3f3 measured this on workspace-1 (gVisor) on 2026-10-08, against the earlier, pydantic-based `workspace-layout` CLI:
  - `python3 layout.py` (the old script): 135 ms.
  - `.venv/bin/workspace-layout`: 582 ms.
  - `uv run --no-sync workspace-layout`: 623 ms.
  - Plain `uv run workspace-layout`: 731 ms.
  - **These numbers no longer describe `workspace-layout`.** #759 has since made its CLI a stdlib-only thin client, precisely because of them, and it hasn't been re-measured. What still holds:
    - `--no-sync` leaves about 40 ms of uv over running the entry point directly;
    - plain `uv run` adds roughly 100-140 ms more for its lock/sync check;
    - top-level imports (pydantic, model building) were the dominant cost, at about 450-560 ms.
  - A resident `uv run` parent also costs about 19 MB per long-running process (commit `4824214bd`).
  - A plain `uv run` rewrote workspace-1's `uv.lock` on 10-07.
- **Launch principles.** Long-running processes `exec` their entry point directly (after a `--frozen` sync). One-offs use `uv run --no-sync`, and keep their top-level imports light.
- **Layout decisions:**
  - A skill's `scripts/` dir is its uv project (`scripts/pyproject.toml`, package `scripts/<skill_name>_skill/`). The workspace glob `.agents/skills/*/scripts` picks it up, because uv requires every glob-matched dir to have a `pyproject.toml` ([uv workspaces docs](https://github.com/astral-sh/uv/blob/main/docs/concepts/projects/workspaces.md)).
  - Entry files stay at today's paths as thin dispatchers. minds, the web client, CI, the guards and update-self's handoff all address scripts by path.
- **Type checking.**
  - One root ty project, checked against the root venv.
  - One bare-tier check against an environment with no third-party packages, so ty's `unresolved-import` *is* the stdlib-only rule.
  - Floor raised to Python 3.12 everywhere.
- **Delivery.** Three stacked template PRs on #831 plus a paired mngr PR. #759 and #814 have merged, so the stack sits on #831 alone, and PR 1's base becomes `main` once #831 merges. The split is for review only; they merge together, so user workspaces migrate once.

## Expected behavior

### For agents and users in a workspace
- One-off commands in docs, skills and prompts read `uv run --no-sync ...`. None of them can trigger a relock or sync, so a drifted lock no longer gets silently rewritten mid-task.
- Skill scripts keep their paths: `.agents/skills/launch-task/scripts/create_worker.py` still exists and still works.
  - Venv-tier skills run as `uv run --no-sync <path>`.
  - Bare scripts still run as `python3 <path>` where config or docs call them.
- An agent facing an arbitrary script doesn't need to know its tier. It runs `uv run --no-sync <path>` unless the instruction it follows says `python3`:
  - a bare script also runs under uv (stdlib only), at about 40 ms more;
  - `python3` on a venv-tier script fails loudly at its first third-party import;
  - the contexts where bareness matters (hooks, pre-sync build steps, program lines, minds/CI/owner-exec calls, update-self's own flow) are wired in config, not typed by agents.
- At session start:
  - If `uv.lock` has drifted from `pyproject.toml`, it is regenerated, and the agent is told (as context, not an instruction to act now) to include it in its next commit.
  - The venv is then synced `--frozen`.
  - With no drift, nothing visible happens.
- Skill-authoring docs say:
  - a skill with scripts gets `scripts/pyproject.toml`;
  - entry modules import only light modules at top level;
  - subcommands import their implementation lazily.
- `validate_skill.py` fails a skill that:
  - is missing `scripts/pyproject.toml`;
  - has the wrong project/package name;
  - has a lock that doesn't include its dependencies;
  - has an entry file that can't import (`--help` fails).
- A user skill whose dependency can't co-resolve with the workspace fails at `uv lock` time, the same as an app.

### For long-running processes
- No `uv` process stays resident as a parent of supervisord or any program.
  - `bootstrap`, github-sync, and the user-service guide use `bash -c "uv sync --all-packages --frozen && exec .venv/bin/<name>"`, like host-backup, share-gateway and env-converge already do.
- A user service whose program line uses `uv run` fails a root test, so the update worker rewrites it during the merge.

### For guards and external callers
- The secrets guard accepts `uv run --no-sync|--frozen|--quiet <script>` as the same call as `uv run <script>`. It still blocks `uv run --env-file ...` or any other option before the script.
- Every path minds, minds_evals, minds_admin, the web client (owner-exec), the mngr create hook or CI calls keeps working unchanged:
  - `system/scripts/{seed_welcome_chat,message_chat,collect_bug_report_diagnostics,forward_port,provision_backups,require_create_account,set_mngr_pin,with_secrets,run_in_background}.py`
  - `.agents/skills/launch-task/scripts/create_worker.py`
  - update-self's staged runner path
- Hook latency per tool call does not regress. Measured by hand before/after; import sets are enforced by test.

### For updates of existing workspaces
- An update into this stack's release changes the root `pyproject.toml`. That triggers the worker's environment gate (`uv lock --check`, then `uv sync --all-packages`) and the full root suite.
  - A user skill with `scripts/` but no `pyproject.toml` makes `uv sync` fail. The worker reads the migration reference and fixes it.
  - Plain `uv run` in user docs, `uv run` program lines in user services, and ty errors in user scripts fail root checks the same way.
- On a pre-2026-09-14 local Lima workspace (system Python 3.11), the staged `update_self.py` exits with a distinct code at Step 3a. The lead reports the update as impossible and offers `migrate-workspace`. Nothing is applied.

### For developers of the template
- The root suite type-checks `.agents`, `system/scripts`, the tests at the top of `system/`, and `conftest.py`, plus a bare-tier check.
- Each project's `test_no_type_errors` is unchanged.

## Implementation plan

### PR 1 -- `gabriel/python-launch-discipline` (template; base `gabriel/ty-version-bump`)

- **`.mngr/settings.toml`**: `extra_window = ["bootstrap='uv run bootstrap'"]` becomes `bash -c "uv sync --all-packages --frozen; exec .venv/bin/bootstrap"` (`;`, so a failed sync still boots). mngr's `NamedCommand.from_string` strips the outer single quotes and runs the rest as the window command.
- **`.agents/skills/github-sync/SKILL.md`** (program line at ~174): `uv run github-sync run` becomes the same exec form.
- **`.agents/shared/references/service-processes.md`**:
  - The example program line (line 22) becomes the exec form.
  - Add the rule "never run a long-running program under `uv run`", linking to `running-python.md`.
- **`system/scripts/build_workspace.sh`** (comment at ~62): manifest-less apps run `.venv/bin/<name>` via the exec form, not `uv run <name>`. Update any docs that tell agents to write such program lines.
- **`SessionStart` sync**:
  - New `system/scripts/session_start_sync.sh`:
    - `uv lock --check`;
    - on failure, `uv lock` and print one line: "`uv.lock` was out of date with `pyproject.toml` and has been regenerated; include it in your next commit.";
    - then `uv sync --all-packages --frozen`.
  - `.claude/settings.json` `SessionStart` calls it instead of `uv sync --all-packages`.
  - `.codex/hooks.json` has no `SessionStart` sync, so nothing changes there.
- **`uv run` → `uv run --no-sync`**, everywhere agents call a command, except skill and system script calls (PR 2 rewrites those):
  - `AGENTS.md`, `.mngr/settings.toml` prompts, `SKILL.md`s and references, `.sh` scripts.
  - Commands: `pytest`, `python`, `mngr`, `app-manifest`, `env-converge`, `host-backup-*`, `tk`, `ruff`, `ty`, `agentic-browser-fleet`, etc.
  - Keep plain `uv run` only where a sync is the point.
- **Guards** (`system/scripts/agent_secrets_guard_check.py` `_allowed_script`):
  - After `uv run`, skip any of `{"--no-sync", "--frozen", "--quiet"}`, then continue as today.
  - Any other option leaves the call unrecognised, so it is blocked.
  - Check `agent_latchkey_request_check.py`, which matches by basename (`word.rsplit("/", 1)[-1]`), and the chat app's `harnesses/tool_output.py` regex, which also matches by basename. Add tests for the `--no-sync` forms; no code change is expected there.
- **New root test `system/test_python_launch_rules.py`**:
  - `test_no_supervisord_program_runs_under_uv_run`: parses `system/supervisord.conf` and `system/supervisord.conf.d/*.conf` (including user-added files) and fails on any `command=` that executes `uv run`.
  - `test_no_plain_uv_run_in_agent_facing_files`:
    - scans tracked `.md`/`.toml`/`.sh`/`.conf` files for `uv run` not followed by `--no-sync`;
    - skips `*/changelog/*`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` and test fixtures;
    - a small explicit allowlist of (file, context) pairs for deliberate syncs.
- **New `.agents/shared/references/running-python.md`**:
  - long-running vs one-off;
  - `--no-sync` and why (measurements, the lock-rewrite hazard);
  - the import-cost rule;
  - the agent rule: run any script with `uv run --no-sync` unless the instruction says `python3`;
  - PR 2 adds the tier rule.
- **`AGENTS.md`**: one line ("run scripts and other one-offs with `uv run --no-sync` unless the instruction says `python3`; never run a long-running process under `uv run`") linking to `running-python.md`.
- **Changelog** entries per touched project (`system/changelog/`, `.agents/changelog/`, and app dirs if touched), checked with `system/scripts/check_changelog_entries.py` with `CHANGELOG_BASE_REF` set to the stack base.
- **This plan file**, `docs/system/blueprint/scripts-as-packages/plan-scripts-as-packages.md`, lives on this branch.

### PR 2 -- `gabriel/skill-scripts-as-packages` (template; base PR 1)

- **Root `pyproject.toml`**:
  - `members` gains `.agents/skills/*/scripts`, `.agents/shared/scripts` and `system/scripts`.
  - Update the members comment.
  - Relock with `uvx uv@latest lock` so the lock stays at revision 5. Local uv 0.11.20 rewrites the lock to revision 3, so don't use it.
- **Per built-in skill with scripts**: build-app, connect-external-service, launch-task, migrate-workspace, notify-user, publish-template, update-app, update-self, use-ai-integration.
  - `scripts/pyproject.toml`:
    - `name = "<skill-name>-skill"`;
    - dependencies taken from the PEP 723 headers (then delete the headers);
    - hatchling, matching the template's other projects.
  - Modules move into `scripts/<skill_name>_skill/`.
  - Entry files stay at their current paths as thin dispatchers: top-level imports are light; each subcommand imports its implementation inside the function.
  - Tests move beside their modules inside the package and import normally. Remove the by-path loading and `sys.path` inserts (e.g. `update-self/scripts/conftest.py`, `create_worker_test.py`).
  - A template-defined table in `scripts/pyproject.toml` (proposed `[tool.workspace.entry-points."<entry>.py"] heavy-imports = [...]`) declares any heavy modules an entry point is allowed to load.
- **update-self**:
  - Package `update_self_skill`.
  - Stubs at `scripts/update_self.py` and `scripts/run_in_background.py` keep the staged-runner path that `launcher_contract_test.py` pins and that older releases' Step 2a calls.
  - The mirrored `tool_env` / `run_in_background` move into the package. `system/scripts/update_self_mirrors_sync_test.py` then compares the package modules with the canonical bare modules.
  - `update_banding.py` keeps its runtime `oom_priority` import from the target tree.
- **`.agents/shared/scripts`**: `pyproject.toml` (`agents-shared`, package `agents_shared`), listed explicitly in `members`. Callers switch to `uv run --no-sync`, including the worker's `eval "$(uv run ... parse_task_frontmatter.py ...)"`.
- **`system/scripts`**: one `pyproject.toml` with two packages.
  - **Bare package** (name TBD): `agent_block_pipe_tail_head_check`, `agent_latchkey_request_check`, `agent_rewrite_bash_command`, `agent_secrets_guard_check`, `agent_tk_standalone_check`, `collect_bug_report_diagnostics`, `install_mngr`, `list_mngr_plugins`, `tool_env`, `seed_welcome_chat`, `message_chat`, `forward_port`, `with_secrets`, `provision_backups`, `require_create_account`, `run_in_background`, `set_mngr_pin`.
  - **Venv package** (name TBD): `check_changelog_entries`, `docs_viewer`, `migrate_claude_auth`, `refresh_workspace_view`, `smoketest_app`, plus test helpers (`guard_testing`).
  - Every current path stays as a stub. Hook stubs keep their one `sys.path` insert for `tk_command_parsing` / `oom_priority` `src/`.
  - Delete `script_modules_testing.py`; its users import the package.
  - `stdlib_only_scripts_test.py` stays until PR 3 replaces it; extend its list to the bare package.
- **`oom_priority/bin`**: entry stubs unchanged; they keep their inserts into `src/`. `script_import_paths_test.py` keeps validating them.
- **Stub path-edit ratchet**: a root test fails on `sys.path` edits anywhere except a declared list of bare stub files and `update_banding.py`. Extend `script_import_paths_test.py` or add to `test_python_launch_rules.py`.
- **Invocation rewrites**:
  - Venv-tier skill and system script calls become `uv run --no-sync <path>`: `create_worker.py` (29 sites), `preview_app.py`, `request_secret.py`, `notify_user.py`, `serve_isolated_instance.py`, `migrate_workspace.py`, `scaffold_flask_lib.py`, `validate_skill.py`, `parse_task_frontmatter.py`, `refresh_workspace_view.py` (update-app `SKILL.md`), etc.
  - Bare calls stay `python3 <path>`.
- **`validate_skill.py`**: replace the PEP 723 check for `run.py` with:
  - `scripts/pyproject.toml` exists, with project `<skill-name>-skill` and package `<skill_name>_skill`;
  - `uv lock --check`;
  - `uv run --no-sync <entry> --help` for every entry file.
- **Skill-authoring docs**:
  - `.agents/shared/references/spec-summary.md` (`run.py` packaging section) and `.agents/shared/worker/references/type-skill.md`:
    - remove "Do NOT add a nested `pyproject.toml`" and the PEP 723 rule;
    - add the package layout, the entry/lazy-import rule, the bash-only `package = false` rule, the co-resolution rule, and when to run `uv lock` / `uv sync --all-packages`;
    - revisit the test-filename-collision rule (likely obsolete once tests sit in uniquely named packages; verify with pytest's import mode).
  - Crystallize/worker docs point at the same.
- **Meta ratchet** (`system/test_meta_ratchets.py`): every `.agents/skills/*/scripts` dir has a `pyproject.toml`.
- **Root tests**:
  - `system/test_entry_point_import_cost.py`:
    - discovers entry files (top-level `.py` in each `scripts/` dir and the `system/scripts` stubs, minus tests/`conftest.py`);
    - imports each in a fresh interpreter (venv tier through the root venv; bare tier through system `python3 -S -s`);
    - fails if a listed heavy module (venv: pydantic, loguru, click, tenacity, httpx; bare: a short list of expensive stdlib modules such as `asyncio`) is in `sys.modules` and not declared.
  - `system/test_external_entry_paths.py`: each externally called stub path runs under `python3 -S -s ... --help` (or a harmless subcommand) and exits 0.
  - `test_python3_invocations_name_bare_entry_points` (in `system/test_python_launch_rules.py`):
    - every `python3 <path>.py` in tracked `.md`/`.toml`/`.sh`/`.conf`/`.json` files must name a file in the declared bare entry-point list;
    - skips the same changelog and fixture paths as the plain-`uv run` check;
    - catches docs still telling agents to run `notify_user.py`, `request_secret.py`, `preview_app.py`, `serve_isolated_instance.py` or `refresh_workspace_view.py` with `python3` after they move to the venv tier.
- **`running-python.md`**: add the tier rule and the package layout. `type-skill.md` / `spec-summary.md` / `service-processes.md` link to it.
- **Migration reference** `.agents/skills/update-self/references/python-packaging-migration.md`, covering:
  - how to convert a user skill (pyproject, package, dispatcher, `--no-sync` calls, bash-only dirs);
  - a user service (program-line form);
  - plain `uv run` in user docs;
  - (PR 3 adds) fixing ty errors in user scripts.

  The changelog entries link to it.
- **Changelog** entries per touched project.

### PR 3 -- `gabriel/type-check-scripts` (template; base PR 2)

- **Root `pyproject.toml`**:
  - `[tool.ty]` with `src.include` covering `.agents`, `system/scripts`, `system/*.py` and `conftest.py`;
  - `ty==0.0.85` in the root dev group (same pin as the projects);
  - `requires-python = ">=3.12"`.
- **`requires-python >=3.12`** in the 16 projects at `>=3.11` and in the new skill/shared/scripts projects. Relock with `uvx uv@latest lock`.
- **Root `system/test_no_type_errors.py`**:
  - root check: `check_no_type_errors` from the repo root;
  - bare check: create an empty venv (`python3.12 -m venv --without-pip` in tmp), then `ty check --python <empty venv> --python-version 3.12 --extra-search-path <bare roots>` over the bare package, update-self's package, `oom_priority/bin` and the `tk_command_parsing` / `oom_priority` `src/` trees. Bare roots: `system/scripts`, `.agents/skills/update-self/scripts`, `system/libs/tk_command_parsing/src`, `system/services/oom_priority/src`.
  - Delete `system/scripts/stdlib_only_scripts_test.py`.
- **Fix the 114 errors** measured at #831's tip (`1f5697934`: `.agents` 20 plus 1 warning, `system/scripts` 19, `system/*.py` 75, `conftest.py` 0), plus anything the restructure moved or exposed. No suppressions unless the code is correct and ty can't express it.
  - The main group is `system/test_app_manifests.py`'s `dict[AppName, ...]` indexed with string literals (about 70).
  - The rest are scattered `invalid-argument-type` / `unresolved-attribute` / `not-subscriptable` / `invalid-method-override` errors.
  - The 12 `unresolved-import`s disappear once the code is packaged.
- **3.12 workaround**: remove `uv python install 3.12` (`setup_system.sh` ~219-228) and `UV_PYTHON=3.12` (`build_workspace.sh` ~40-45, `install_dependencies.sh` ~22) only after a fresh Lima (Debian 13) build works without them.
- **update-self version guard**:
  - `scripts/update_self.py` stub: before importing `update_self_skill`, if `sys.version_info < (3, 12)`, print the impossible-update message and exit with a dedicated code.
  - `SKILL.md` §3a gets the note: on that exit, stop, report the update as impossible, and offer `migrate-workspace`.
  - The stub's top must parse on 3.11.
- **Migration reference**: add the "fix ty errors in user scripts" step.
- **Changelog** entries per touched project, including all 19 projects for the `requires-python` bump.

### Paired mngr PR -- `gabriel/skill-scripts-as-packages` (mngr; base main)

- `apps/minds_evals/imbue/minds_evals/testing.py:1783` `uv run .agents/skills/launch-task/scripts/create_worker.py` becomes `uv run --no-sync ...`.
- Any minds_admin demo `uv run` call into template scripts gets the same change. The 23f0b3f3 work already converted `workspace-layout`.
- minds' bare `python3 <path>` calls (`welcome_chat.py`, `chat_app.py`, `workspace_diagnostics.py`, `skill_chat.py`) are unchanged.
- Run minds_evals' and minds_admin's tests. CI's `build-minds-snapshot` uses the same-named template branch (PR 2).
- mngr changelog entries.

## Implementation phases

1. **Plan and branches.**
   - This plan sits on PR 1's branch.
   - Branches: `gabriel/python-launch-discipline` → `gabriel/skill-scripts-as-packages` → `gabriel/type-check-scripts` (template); `gabriel/skill-scripts-as-packages` (mngr).
   - Template reviewer `base_branch` points at each PR's parent.
2. **Baseline measurements**:
   - create the dev-tier imbue-cloud scratch workspace at the latest `minds-v*` tag;
   - record `--help` timings (median of 15) for every entry point that will change, hook entry points included;
   - record per-process RSS of supervisord's programs and any resident `uv`.
3. **PR 1.**
   - Guard allowlist with tests first (red, then green); program-line and plain-`uv run` root tests (red against today's tree).
   - Then the edits that turn them green: program lines, `session_start_sync.sh`, `--no-sync` sweep, `running-python.md`, `AGENTS.md`.
   - The system works at the end: nothing changes layout.
4. **PR 2, smallest first**:
   1. `system/scripts` packages and stubs.
   2. `.agents/shared/scripts`.
   3. One simple skill end to end (notify-user) to prove the pattern: glob, lock, stubs, tests, `validate_skill.py`.
   4. The remaining skills.
   5. update-self last, its staged-runner contract tests guarding it.
   6. The import-cost, external-path and stub-path-edit tests, and the meta ratchet.
   7. Docs and the migration reference.

   Each step leaves the suite green.
5. **PR 3**:
   - root `[tool.ty]` and the bare check (red with the current count);
   - fix the errors to green;
   - the 3.12 floor;
   - the update-self guard with its test;
   - verify a fresh Lima build without the workaround, then remove the workaround.
6. **Paired mngr PR**, pushed after PR 2's template branch so CI pairs correctly.
7. **Manual validation on the scratch workspace**:
   - plant a user skill (PEP 723 scripts, plain `uv run` in its `SKILL.md`) and a user service under `uv run`;
   - update-self to the stack tip;
   - verify the worker migrated both, all programs RUNNING, the root checks pass, no resident `uv`;
   - then record the "after" timings and RSS.

## Testing strategy

- **Guard allowlist (PR 1)**, unit tests in `agent_secrets_guard_check_test.py`:
  - `uv run --no-sync .../request_secret.py --file ...` and `uv run --frozen --quiet .../with_secrets.py ... -- cmd` are recognised;
  - `uv run --env-file x .../with_secrets.py` and `uv run --with pkg ...` are not, so they're blocked;
  - matching cases in `agent_latchkey_request_check_test.py` and the chat app's tool-output tests.
- **Program-line rule**: the test fails on a planted `command=... uv run x` conf in a tmp copy and passes on the real tree; it also covers the `oom_tag_service.py ... uv run` wrapping form.
- **Plain-`uv run` rule**:
  - the test catches a planted plain `uv run` in a `.md`;
  - it ignores changelogs and fixtures;
  - every allowlisted entry still exists (no stale exceptions).
- **`session_start_sync.sh`** (in a tmp copy of a minimal uv project):
  - in-sync lock → no output, exit 0;
  - drifted lock → the lock is regenerated, the one-line message printed, the sync succeeds.
- **Import-cost tests**:
  - prove the probe can fail: a planted `import pydantic` at an entry's top level fails the test;
  - a declared heavy import passes;
  - a lazy import inside a subcommand passes.
- **External entry paths**:
  - each stub runs under `python3 -S -s ... --help` and exits 0;
  - a planted third-party import in a bare module makes it fail.
- **`python3` invocations**:
  - a planted `python3 .agents/skills/notify-user/scripts/notify_user.py` in a tmp doc fails the check;
  - `python3 system/scripts/forward_port.py` passes;
  - a planted `python3` call to a nonexistent path fails, so stale paths are caught too.
- **Bare ty check**: a planted `import yaml` in a bare module gives `unresolved-import`, and 3.12-only syntax is accepted (the floor is 3.12).
- **update-self guard**:
  - the stub run with a faked `sys.version_info < (3, 12)` (e.g. via `-c` with a patched `sys`, or a 3.11 interpreter if available) exits with the dedicated code and the message naming `migrate-workspace`;
  - the existing `launcher_contract_test.py` / `staged_runner_test.py` still pass.
- **Meta ratchet**: fails on a tmp skill with `scripts/*.py` and no `pyproject.toml`, and on a bash-only `scripts/` with no `pyproject.toml`.
- **`validate_skill.py`**:
  - `ok` on every built-in skill;
  - fails on a missing pyproject, a wrong package name, a stale lock, or an entry that can't import.
- **Existing suites**:
  - each project's ratchets and `test_no_type_errors`;
  - the root suite;
  - chat and system_interface suites, which the guard/tool-output edits touch;
  - full CI via the template's `test-offload`;
  - the mngr PR's CI with the paired template branch.
- **Manual (scratch workspace)**:
  - the update-self migration run;
  - `ps -eo ppid,pid,rss,args` shows no `uv` parent;
  - every program RUNNING;
  - before/after timing and RSS tables in the PR descriptions (median of 15; hooks included);
  - a fresh Lima build without the 3.12 workaround.

## Open questions

- Resolved in PR 2:
  - `system/scripts` packages are `workspace_bare_scripts` and `workspace_scripts`, in one hatchling project (`packages = [...]`). The editable install's `.pth` adds the whole `scripts/` dir to `sys.path`, which exposes both packages (and, as a side effect, the entry stubs as top-level module names).
  - The entry-point table is `[tool.workspace-template.entry-points."<entry>.py"]` with `heavy-imports = [...]`.
  - The test-filename-collision rule is dropped: pytest imports a test inside `<skill>_skill/` as `<skill>_skill.<test>`, and two same-named tests in two skill packages collect together (two flat `scripts/` dirs still collide).
  - A venv-tier stub run through the `.claude/skills` symlink path is not recognised as a workspace member (uv builds an empty `scripts/.venv`), and listing the symlinked glob as a member is refused as a duplicate. `running-python.md` says to run skill scripts by their `.agents/skills/` path.
- **Expensive-stdlib list for the bare tier**: start with `asyncio`, then measure on the scratch workspace before fixing the list.
- **Does update-self's apply health probe cover every program?** If a bare wrapper broke after an apply, would the probes catch it? Relevant only to the safety margin. The 3.12 guard makes it moot for the version case.
- **#831 is still open**: merge its tip into the stack as it moves. #759 (thin `workspace-layout` client and its `--no-sync` changes) and #814 are already merged, and PR 1 inherits them through #831's main merge; don't redo their conversions.
- **Follow-ups outside this stack**: MIND-485 (guard dispatcher and the rewrite hook's `Bash` matcher, on main after this merges); MIND-486 (migration-doc system that reshapes the migration reference).
