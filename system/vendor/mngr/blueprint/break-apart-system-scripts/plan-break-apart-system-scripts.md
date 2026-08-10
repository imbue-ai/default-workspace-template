# Plan: Break apart default-workspace-template's system/scripts

## Refined prompt

> Break apart default-workspace-template's `system/scripts/`, moving cohesive script clusters into proper packages. A non-backwards-compatible cutover is allowed (all users get new hosts). Work spans the dwt repo (via `.external_worktrees/` per CLAUDE.md) and the mngr monorepo (eval launcher). One PR per repo, both on branch `mngr/break-apart-scripts`.
>
> * **Eval worker**: move `eval_responder.py`, `eval_sink.py`, `eval_decider.py`, `eval_wait_watcher.py` (+ test) into a new `system/services/eval_worker/` package (`eval-worker` console script; supervisord block renamed to `[program:eval-worker]`); move `boto3` from root deps into the package.
> * The slotted metadata file stays at a tracked location inside the package (`system/services/eval_worker/test_case_metadata.json`) since the eval launcher delivers it by committing it into a per-case clone and `data/` is gitignored; the done marker moves from the stale `runtime/eval_done` to `data/.state/eval/done`.
> * **Automations machinery**: move `run_job.sh`, `with_agent_env.sh`, and `run_schedule_agent.sh` (renamed `run_automation.sh`) plus `run_job_test.py` into `system/libs/automations/`; move `caretaker_check.sh` into `system/services/caretaker/`, which owns the caretaker docs. Keep all four as bash -- no Python rewrite.
> * Rename the schedule-agent identifiers to the automation vocabulary: create template `schedule_agent` -> `automation`, label `schedule_agent=<skill>` -> `automation=<skill>`.
> * `caretaker_check.sh` locates the automations script via repo-root derivation from its own path, not hardcoded absolute paths.
> * Amend `system/services/README.md` to cover cron-driven (not just supervised) services; bash-only packages get stub `pyproject.toml` files so they are real uv-workspace members and changelog-gate projects.
> * Update the "automation" vocabulary docs -- `README.md`, `CLAUDE.md`, `docs/system/workspace-internals.md`, and the manage-scheduled-tasks skill -- to reference `system/libs/automations` as how automations run, with the caretaker as the built-in example; the automations README documents the pieces and points at the skill for the recipe (no duplication).
> * **OOM entry points**: move the five entry scripts (+ tests) into `system/services/oom_priority/bin/`, keeping them bare-python3 runnable with `sys.path` inserts shortened to `parents[1] / "src"`; move and generalize `script_import_paths_test.py` to scan both `system/scripts/` and the new `bin/`.
> * **Terminal**: move `notify_terminal_session.py` and `terminal_tmux.conf` into `system/apps/terminal/`.
> * **GitHub sync**: move `git_hooks/post-commit` into `system/libs/github_sync/`, updating the `wire-git` hooksPath wiring.
> * Update all path references: `system/supervisord.conf`, `.mngr/settings.toml` (claude/worker `command`, main-template tmux.conf line, caretaker/automation templates), `.claude/settings.json` (shed-notice hook), the caretaker/enable-caretaker/disable-caretaker/manage-scheduled-tasks skills, and the `system/scripts/README.md` + `system/README.md` listings.
> * **mngr monorepo side** (`apps/mngr_minds_eval`): update the metadata path constant, switch the default template branch to `main`, drop the pre-restructure layout fallbacks, and fully rename FCT terminology to default-workspace-template/dwt -- CLI flags become `--dwt-repo`/`--dwt-branch` (`box --dwt-link` unified to `--dwt-repo` too), config keys become `dwt_repo`/`dwt_branch`; drop the pinned `fct_branch` keys from the checked-in `eval-config*.json` examples (rely on the new `main` default); configs + README updated.
> * Landing order: the dwt PR merges first, then the mngr eval PR (the default-branch switch to dwt `main` only works once the new layout is on `main`).
> * Out of scope: `layout.py`, `forward_port.py`, the `claude_*` hooks, provisioning/boot scripts.

## Overview

- `system/scripts/` in default-workspace-template has become a catch-all: supervised services (the eval worker), cron-driven machinery (the caretaker + recurring-job runner), package entry points (the OOM scripts), and app-owned helpers (terminal tmux wiring, the github-sync git hook) all sit next to genuine provisioning scripts.
- The template already has the right homes -- `system/services/` (background services), `system/libs/` (support libraries), `system/apps/` (tab apps) -- so this is a relocation into the existing structure, not a new architecture.
- A non-backwards-compatible cutover is allowed (every user gets a new host), so all path contracts (supervisord, `.mngr/settings.toml`, `.claude/settings.json`, skills, cron lines) are updated in place with no compatibility shims.
- The move also aligns naming with the workspace vocabulary: the recurring-job machinery becomes `system/libs/automations/` (an "automation" is a skill run on a schedule), and the eval launcher in the mngr monorepo finally drops the retired "FCT" (forever-claude-template) terminology.
- Two PRs on branch `mngr/break-apart-scripts`: one in default-workspace-template (all moves + doc updates), one in the mngr monorepo (`apps/mngr_minds_eval` path constant + FCT rename). The dwt PR lands first.

## Expected behavior

- No user-visible behavior changes: eval runs, the caretaker, OOM shedding, terminal tab titles, and github-sync auto-push all work exactly as before on newly created hosts.
- `system/scripts/` shrinks to what its README claims it is: provisioning/build scripts, boot-recovery scripts, Claude Code hooks, and repo-dev tooling (changelog gate, reviewer settings).
- The eval worker is a normal workspace package: `[program:eval-worker]` runs `uv run eval-worker`; `boto3` is a dependency of `system/services/eval_worker/` only, not of the workspace root; the worker still no-ops when its slotted `system/services/eval_worker/test_case_metadata.json` is absent; its terminal done marker lives at `data/.state/eval/done` (runtime-written, gitignored, never committed).
- Recurring jobs run through `system/libs/automations/`: cron lines invoke `with_agent_env.sh` + `run_job.sh` from there, and singleton schedule agents are woken by `run_automation.sh` and found by the `automation=<skill>` label using the `automation` create template. The caretaker is the built-in example, with its deterministic check at `system/services/caretaker/caretaker_check.sh`.
- Enabling the caretaker (via the enable-caretaker skill) writes a cron line with the new absolute paths; the durable copy under `data/.state/cron.d/` on old hosts is irrelevant because old hosts are discarded.
- The five OOM entry points run from `system/services/oom_priority/bin/` under bare `python3` exactly as before: supervisord command prefixes, earlyoom's `-N` absolute-executable path, the claude launch `command`, and the SessionStart shed-notice hook all point at the new paths.
- Terminal tab-title tracking works from the terminal app's own folder: `~/.tmux.conf` sources `system/apps/terminal/terminal_tmux.conf`, whose hooks call `system/apps/terminal/notify_terminal_session.py`.
- `github-sync wire-git` points `core.hooksPath` at `system/libs/github_sync/git_hooks/`.
- The vocabulary docs (workspace `README.md`, `CLAUDE.md`, `docs/system/workspace-internals.md`, manage-scheduled-tasks skill) explain that automations run via `system/libs/automations`, caretaker as the example; the automations README describes the pieces and defers the "add a job" recipe to the skill.
- On the mngr side, `minds-evals` speaks default-workspace-template: `launch --dwt-repo/--dwt-branch`, `box --dwt-repo/--dwt-branch`, config keys `dwt_repo`/`dwt_branch`, default branch `main`, and no pre-restructure (`scripts/` at repo root) layout fallbacks. Old flag/key spellings stop working.

## Changes

### default-workspace-template repo (worked in `.external_worktrees/default-workspace-template`, branch `mngr/break-apart-scripts`)

- New package `system/services/eval_worker/`: the four eval modules (+ decider test) with a `pyproject.toml` owning the `boto3` dependency and an `eval-worker` console script; root `pyproject.toml` drops `boto3` and swaps the `eval` dependency wiring to the new package; done-marker path updated to `data/.state/eval/done`; metadata path updated to the package dir; supervisord block renamed `[program:eval-worker]` and pointed at the console script.
- New package `system/libs/automations/` (stub `pyproject.toml`, bash scripts): `run_job.sh`, `with_agent_env.sh`, `run_automation.sh` (renamed from `run_schedule_agent.sh`, label + template identifiers renamed to `automation`), `run_job_test.py`, and a README describing the machinery and pointing at the manage-scheduled-tasks skill.
- New package `system/services/caretaker/` (stub `pyproject.toml`): `caretaker_check.sh` (repo-root-relative call into the automations lib) plus a README owning the caretaker's design notes; `system/services/README.md` amended to cover cron-driven services.
- `.mngr/settings.toml`: `[create_templates.schedule_agent]` renamed to `[create_templates.automation]`; caretaker template comments and all script paths updated.
- Skills updated: `caretaker`, `enable-caretaker`, `disable-caretaker`, `manage-scheduled-tasks` (new paths, `automation=<skill>` label, `run_automation.sh`).
- OOM entry points moved to `system/services/oom_priority/bin/` with `sys.path` inserts shortened; `script_import_paths_test.py` moved/generalized to scan both `system/scripts/` and the new `bin/`; wiring updated in `system/supervisord.conf` (four command prefixes, the earlyoom `-N` path, the event-listener command), `.mngr/settings.toml` (claude + worker `command`), and `.claude/settings.json` (shed-notice SessionStart hook).
- Terminal files moved to `system/apps/terminal/`; the main-template tmux.conf `if-shell`/`source-file` line and the conf's two hook lines updated.
- `git_hooks/post-commit` moved to `system/libs/github_sync/git_hooks/`; the `wire-git` hooksPath constant updated.
- Vocabulary docs updated (workspace `README.md`, `CLAUDE.md`, `docs/system/workspace-internals.md`) to link automations to the new lib; `system/scripts/README.md` and `system/README.md` listings trimmed to what remains.
- uv workspace: new members picked up by the existing `system/libs/*` / `system/services/*` globs (stub pyprojects make the bash-only dirs valid members); changelog entries added for every touched project (new packages, oom_priority, terminal app, github_sync, agents bucket for skill edits, dev bucket for the scripts removals and root config).

### mngr monorepo (branch `mngr/break-apart-scripts`)

- `apps/mngr_minds_eval`: metadata write path becomes `system/services/eval_worker/test_case_metadata.json`; `_workspace_system_dir` and other pre-restructure layout fallbacks removed; default template branch switched to `main`; FCT terminology fully renamed to dwt (`DEFAULT_FCT_*` constants, `--fct-repo`/`--fct-branch` -> `--dwt-repo`/`--dwt-branch`, `box --dwt-link` -> `--dwt-repo`, config keys `fct_repo`/`fct_branch` -> `dwt_repo`/`dwt_branch`); the checked-in `eval-config*.json` examples drop their pinned branch keys; README/SETUP docs updated; changelog entry for `apps/mngr_minds_eval`.
- Landing order: the dwt PR merges first; the mngr PR merges after, since its `main` default requires the new layout on dwt `main`.
