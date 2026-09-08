---
name: tmr-behaviors
description: Run `mngr tmr-behaviors` on a behavior corpus (one mapper agent per .feature file, then a reducer) and land the integrated result as a stacked branch/PR. Covers the required host setup (Claude trust, skip-permissions agent type), invocation gotchas, flaky-launch recovery, and post-run integration. Invoke with /tmr-behaviors.
---

# Running tmr-behaviors on a behavior corpus

This skill is the runbook for turning a behavior corpus (`<project>/behaviors/`) into witnessing tests via `mngr tmr-behaviors`. Read `libs/mngr/docs/commands/secondary/tmr-behaviors.md` for the command's own documentation first.

## Step 0: Prerequisites (host config, once per machine)

Both of these are required; without them every mapper launch fails or stalls. Check before launching:

1. **Claude Code trust for the agent copy dirs.** Mappers work in fresh per-agent copies under `~/.mngr/copies/agent-<id>`; Claude Code's trust dialog otherwise eats the initial prompt (`Source directory ... is not trusted by Claude Code`). Trust the parent dir once -- mngr's trust check accepts an ancestor:

   ```bash
   uv run python -c "
   from pathlib import Path
   from imbue.mngr_claude.claude_config import add_claude_trust_for_path
   add_claude_trust_for_path(Path.home() / '.claude.json', Path.home() / '.mngr' / 'copies')
   "
   ```

2. **A skip-permissions agent type.** The default `claude` agent type runs interactively and mappers stall on permission-approval dialogs. Register a `yolo` type at user scope and pass `--agent-type yolo`:

   ```bash
   uv run mngr config set agent_types.yolo.parent_type claude --scope user
   uv run mngr config set agent_types.yolo.cli_args '["--dangerously-skip-permissions"]' --scope user
   ```

Warn the user before changing their config; these are user-scope, persistent changes.

## Step 1: Prepare the stacked base

1. Agents inherit the checkout state as their base branch. Before launching, have a checked-out commit stacked on top of the corpus branch holding the setup files (the mapper prompt and changelog entry from the next steps). If `.jj/` exists, use the jujutsu skill to manage the stack; otherwise use plain git.
2. Create the project-specific mapper prompt at `libs/<project>/tmr/behaviors_mapper.j2`, additively extending the packaged template (`{% extends "behavior_mapper.j2" %}`, filling the `project_guidance` and `infra_blockers` blocks). Copy the pattern from `apps/minds/tmr/behaviors_mapper.j2` or `libs/mngr_claude/tmr/behaviors_mapper.j2`. The prompt must state: generate tests for every unwitnessed unit (CREATE_TEST), drive every unit to FULL (PARTIAL_STEADY only for residue untestable in kind), and arbitrate failures by the behavior (FIX_IMPL when the implementation diverges, FIX_TEST when a test over-asserts).
3. Add the per-project changelog entry (`<project>/changelog/<branch-name>.md`) -- the CI changelog gate requires it.
4. Validate the corpus before spending agent time: `uv run mngr behaviors list --root <project>/behaviors` (fail-fast on violations).

## Step 2: Launch

```bash
MNGR_HEADLESS=1 nohup uv run --project libs/mngr_tmr mngr tmr-behaviors \
  --root libs/<project>/behaviors \
  --name tmr-behaviors-<slug> \
  --mapper-prompt libs/<project>/tmr/behaviors_mapper.j2 \
  --agent-type yolo \
  --output-dir tmr_behaviors_<slug>_run \
  > /tmp/tmr-behaviors-<slug>.log 2>&1 &
```

Gotchas:

- `--headless` is rejected as a global flag; use the `MNGR_HEADLESS=1` env var.
- Name `--output-dir` with underscores: `.gitignore` covers `**/tmr_*/` but not dash-named dirs, and the run artifacts must not end up committed.
- Run in the background and poll the log; a full run is tens of minutes to hours.
- Ignore the cosmetic `ttyd` install 404 warning.
- On macOS with Claude.ai OAuth credentials, expect the `isolate_local_config_dir` staleness warning. Runs under ~1h have been fine; for longer runs consider `mngr config set agent_types.claude.isolate_local_config_dir false --scope user` (ask the user first).

## Step 3: Monitor and handle flaky launches

- Watch for `Failed to launch agent for <file>` / `Timeout waiting for message submission evidence`. Under load the last-launched mapper flakes; there is no single-file rescope (filters are only `--area`/`--tag`/`--unit`). If a mapper fails to launch, finish the run, integrate, then do a follow-up run stacked on the integrated result (already-witnessed files report no-change and finish fast).
- Verify mappers are actually working with `tmux capture-pane -t mngr-<agent-name> -p`; a permission-approval dialog means the yolo type was not used.
- When a mapper finishes, read its `testing_agent_outcome.json` under the output dir: check per-unit verdicts, that any PARTIAL_STEADY is genuinely untestable-in-kind (not merely expensive), and that `blockers`/`behavior_problems` are empty or understood.

## Step 4: Integrate the reducer branch

1. The reducer publishes `<name>/<run>/reducer` as a git branch. Fetch it locally, then check its base: it may sit on the corpus tip rather than your setup commit. If so, restack the setup commit on top of the reducer branch so the result is one stacked chain: corpus branch, integrated run results, setup commit on top.
2. Verify the corpus is untouched: diffing the reducer branch against its merge-base with the base branch must show no changes under `<corpus-root>` (the recipe gates this, but check).
3. Read the reducer's `integrator_outcome.json`: normalizations, escalations, and the verified `matrix.jsonl` coverage.
4. The reducer writes changelog entries named after mapper branches; rename/merge them into `<project>/changelog/<pr-branch>.md` for every project the run touched (the fix may span projects, e.g. libs/mngr + libs/mngr_claude).
5. Run `uv run ruff format` on the touched files -- agent code has shipped unformatted and failed `test_no_ruff_errors` in the full suite.
6. Run the touched projects' unit tests locally, and re-run any release/e2e witnesses yourself on the integrated tree: the reducer may not be able to execute them in its environment (it escalates this), and release tests do not run in CI.

## Step 5: Ship

- A branch named after the PR exists and points at the tip containing the integrated run results and the setup commit.
- The branch is pushed to origin, and a draft PR is open for it, based on the corpus branch (or main if the branches were folded into one).
- The full test suite (`just test-offload`) has been run on the final tree and passes.

Use the repo's VCS to accomplish this: if `.jj/` exists, use the jujutsu skill; otherwise plain git.
