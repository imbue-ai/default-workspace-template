# Nested worker dispatch: decouple launch-task from harden-worker

## Overview

- `launch-task` is the generic mechanism for running a task in an isolated worker: create a sub-agent in its own git worktree, hand it a task file, poll for the report file it pushes back. Today it is welded to the harden worker: every `-t worker` create installs `.agents/shared/worker/` as a `harden-worker` skill, the worker finds its task file by a single-match glob, and the lead-side and worker-side prose assume one flat level (one lead, one worker, no children).
- This work makes dispatch **level-agnostic**. A lead is any agent that runs `create_worker.py launch` and polls for a report; a worker is any agent that reads a task file and pushes a report. A worker may be a lead. Nothing in the contract depends on which level an agent is at.
- Three contract changes carry that: the launcher stamps the task file's own exact path into its frontmatter (no glob); the launcher labels every worker with its lead, so a lead's idle detection can tell "finished without reporting" from "waiting on its own children"; and worker-side reporting becomes a launcher subcommand every worker calls instead of prose every worker re-derives.
- The harden worker is no longer installed as a skill. A harden task file tells the worker to follow `.agents/shared/worker/SKILL.md` from its own checkout, which it already has. The provisioning script and its template line are deleted.
- The harden worker may split its pass across parallel sibling workers. This is one paragraph of permission and constraints in the harden contract, not a new mode: the lead decides the split, siblings run only their scope's tests, and the lead runs the full suite and both review gates once on the merged result.
- Lead prompting stays minimal. The lead is a capable agent that makes decisions; the procedures encode only what it cannot know: orchestration steps and infrastructure requirements.
- Correctness is proven at three levels: contract tests over the prose and scripts (no mngr); a live claude release test of a two-level dispatch with a `question` round trip; and a minds_evals comparison of a `direct` harden pass against a `parallel` one on the todo app.

## Expected behavior

**Unchanged for a top-level lead**

- A chat agent dispatching a harden pass or a plain delegated task runs the same commands as today: write a task file, `create_worker.py launch`, background `create_worker.py await`, handle the report, merge on `done`. The launch command keeps its `create_worker.py launch --name <x> ... --task-file <f>` shape, which minds_evals uses to discover workers.
- A direct harden pass behaves as today; the operation and type references are not edited beyond adding `question` to their valid report names.
- `await` still exits 0 with the report, 124 on timeout, 75 on OOM shed, 76 on idle-without-report.

**New in the task-file contract**

- `launch` stamps `task_file: <path relative to the repo root>` into the frontmatter, as it already stamps `lead_agent`, before sending the file as the worker's message. The worker reads its exact path out of the message it received and passes it to `parse_task_frontmatter.py`, which no longer accepts globs and also emits `TASK_FILE`.
- `launch` adds `--label lead_agent=<launching agent>` to every create.
- `launch` syncs the runtime dir with `--uncommitted-changes=clobber` rather than `merge`. The destination is under gitignored `data/`, so nothing tracked is overwritten and no stash entry touches the stack that every worktree of the repo shares.
- The harden task bodies say "follow `.agents/shared/worker/SKILL.md`" instead of "use the installed `harden-worker` sub-skill". `finish_report_path` stays the only author-supplied field a plain delegated task needs.

**Reporting**

- A worker reports with `create_worker.py report --task-file <p> --type gate|status --name <x> --body-file <f>`. The subcommand writes `report.md` beside `finish_report_path` in the worker's tree and pushes that directory to the lead with `--uncommitted-changes=clobber`. If the push fails or `lead_agent` is missing, it resolves the worker's own `lead_agent` label from `mngr list`, then that lead's work dir, and copies the file there. If neither resolves it fails loudly: a report that cannot be delivered is an error, not a silent copy somewhere.
- `await` archives the report it prints into `consumed/` (timestamped, named by kind) so a relaunch after a gate never trips the stale-report guard and the lead never moves files by hand.
- Any worker may emit a mid-flight `type: gate`, `name: question` report; it is valid for every operation. An intermediate lead answers what it can from the task file and repo; otherwise it re-raises the question as its own `question` gate to its lead and forwards the answer back down verbatim.

**Nested dispatch**

- A worker that launches a sub-worker uses launch-task exactly as a chat agent does. Its `MNGR_AGENT_NAME` becomes the sub-worker's `lead_agent` (stamped and labelled), the sub-worker's runtime dir lives in the intermediate lead's worktree, and the sub-worker's report lands in that worktree. Paths never collide across levels because every path is exact.
- A lead's `await` treats its worker as busy while the worker has a live child: an agent labelled `lead_agent=<worker>` whose state is RUNNING or WAITING and that has no pending OOM shed. Only when no such child exists does the idle count run.
- Merge is a strict tree: a sub-worker's branch `mngr/<sub>` is merged only into its direct lead's branch, with `--no-ff` and the sub-worker named in the merge commit message. The top-level lead sees one merged branch and does not need to know sub-workers existed.
- An intermediate lead destroys neither its sub-workers nor itself; they stay stopped in place after merge, so their transcripts and pushed reports remain where a later capture resolves them.
- An intermediate lead awaits its sub-workers with `--timeout 60m`, inside the top-level lead's 90m.

**Parallel harden pass**

- The harden contract permits the worker to split its pass across sibling workers launched via launch-task. The worker decides the split for the creation at hand (for a Flask app: backend and frontend), writes one task file per sibling with the boundary described in prose, and states in each task body that the sibling runs only its scope's tests and skips the review gates because its lead runs them on the merged result. A sibling may make small out-of-scope edits and lists them in its `done` report.
- When a sibling's `question` decides a shared interface, the lead uses its judgement per case; messaging the affected sibling immediately with the decision is the documented default.
- The lead merges the siblings in a fixed order, resolves conflicts itself, then runs the full suite, ratchets, and both review gates once on the merged result before reporting `done` with the same body a direct pass would.

**Removed**

- `.agents/shared/scripts/install_worker_skills.sh`, its test, and the `worker` template's install line. The template keeps the stop-hook env, git-worktree transfer, venv converge, and plugin install (a direct worker still runs the review gates).
- The `<TASK_FILE_GLOB>` substitution in task bodies. The worker already holds its exact path.
- The `mkdir/mv` consume snippet and the `git worktree list | head -1` fallback in the reporting prose.

**Evals**

- Two arms, `direct` and `parallel`, three attempts each, on the todo-app harden config. The arms are two branches of this repo that differ only by one sentence in the crystallize lead's task body asking for a parallel split. A smoke run precedes the comparison: the probe config, then one `direct` trial whose worker transcripts, reports, and harness failure signatures are reviewed by hand.
- Success is judged on deterministic metrics: gates at 1.0 on every attempt, zero worker harness failure signatures, and mean wall-clock lower for `parallel`. Judge scores are reported, not thresholded.

## Implementation plan

### Launcher: `.agents/skills/launch-task/scripts/create_worker.py`

- `_ensure_task_file_path(task_file)`: stamps `task_file` via `_set_frontmatter_field`, with the path made relative to `git rev-parse --show-toplevel`; mirrors `_ensure_lead_agent`.
- `launch`: adds `--label lead_agent=<lead>` to `mngr create` (the same value `_ensure_lead_agent` stamps); `rsync_dir` uses `--uncommitted-changes=clobber`; the docstring is rewritten for the level-agnostic contract.
- `_worker_is_idle(worker_name, runner)`: after reading the worker's own state from `mngr list --format jsonl`, scan the same output for records whose `labels.lead_agent == worker_name` with state RUNNING or WAITING and no pending shed (`_worker_has_pending_shed`); any such child means not idle. Still one `mngr list` call per poll.
- `await_report`: after printing a report, archive it with `_archive_report` (already used by `launch_sync`), naming the archive by timestamp and the report's `type`/`name`.
- New `report` subcommand: `report_to_lead(task_file, report_type, name, body_file, runner)` reads `finish_report_path` and `lead_agent` with `_read_frontmatter_field`, writes the report, pushes with `rsync_dir`'s conventions to `<lead>:<dir>/`, falls back through `mngr list` (the worker's own record's `labels.lead_agent`, then the lead's `work_dir`), and raises when no destination resolves. Prints the destination used.
- `launch-sync` and `destroy` unchanged. No new flags on `launch` or `await`.

### Worker-side parser: `.agents/shared/scripts/parse_task_frontmatter.py`

- Positional argument is an exact path; `resolve()` and the glob handling are removed.
- `task_file` joins `lead_agent` and `finish_report_path` as a well-known field, emitted as `TASK_FILE`.

### Config: `.mngr/settings.toml`

- `[create_templates.worker]`: remove `bash .agents/shared/scripts/install_worker_skills.sh .agents/skills` from `extra_provision_command__extend`. Nothing else changes.

### Code dependents (must change with the code)

- `.gitignore`: the `.agents/skills/*-worker/` rule and its comment are deleted with the install script.
- `.agents/shared/scripts/install_worker_skills.sh` and `install_worker_skills_test.py`: deleted.
- `system/test_mngr_template_stacking.py`: the worker-template test asserts the install line is gone.
- `.agents/skills/launch-task/scripts/create_worker_test.py`: `task_file` stamping (repo-root relative, from a subdirectory too), the `lead_agent` label and its live-CLI contract, `clobber` on the runtime sync, idle tolerance with a labelled child in each of RUNNING, WAITING, STOPPED, and shed, `await` archiving, and `report` (push argv, fallback resolution, failure when unresolvable).
- `.agents/shared/scripts/parse_task_frontmatter_test.py`: exact path only, `TASK_FILE` emission.
- `.agents/skills/launch-task/scripts/dispatch_contract_test.py`: dispatchers are discovered only among `SKILL.md` files under `.agents/skills/`; glob assertions are replaced by "the produced task file parses through the worker parser at its exact path"; the launch on the real task file asserts the `lead_agent` label and the stamped `task_file`.
- `.agents/skills/update-self/scripts/launcher_contract_test.py`: unchanged; the update-self prose asks nothing new of the launcher.
- New `.agents/skills/launch-task/scripts/test_nested_dispatch_live.py` (`release`, `tmux`): see Testing strategy.

### Prose dependents (working during development; human rewrite once the branch is complete and tested)

- `.agents/skills/launch-task/SKILL.md`: the stamped `task_file`, the `lead_agent` label, `report` and `await` archiving, and that the same steps apply when the launching agent is itself a worker. The task body's reporting section names the exact path instead of a glob.
- `.agents/shared/references/worker-reporting.md`: shrinks to the `report` call, the report body shapes, and "the push is the ready signal".
- `.agents/shared/references/lead-proxy.md`: the intermediate-lead rules (answer-or-re-raise a child's `question`; forward answers verbatim), `await` archiving in place of the consume snippet, the idle-tolerance note, the `clobber` rationale, the sub-worker lifecycle (stop in place, never destroy), and the merge commit naming the worker.
- `.agents/shared/worker/SKILL.md`: Step 1 parses the exact path from the message; Step 3 says `question` is valid on every run.
- `.agents/shared/worker/references/harden-creation.md`: the parallel-pass paragraph (permission, the lead decides the split, siblings run scoped tests and no gates, the lead merges in order, resolves conflicts, and runs the full suite, ratchets, and both gates once).
- `.agents/shared/worker/references/op-crystallize.md`, `op-update.md`, `op-heal.md`: `question` added to every shape's valid names; the crystallize clause tying it to the creation reference removed.
- `.agents/shared/worker/references/verification.md` and `type-system-interface.md`: the sentences that say `question` exists only where an operation defines it.
- `.agents/skills/crystallize-creation`, `update-creation`, `heal-creation`, `update-system-interface`, `build-app`: "use the installed `harden-worker` sub-skill" becomes "follow `.agents/shared/worker/SKILL.md`"; the sentence "the `worker` template installs the generic `harden-worker` sub-skill" is removed.
- `.agents/skills/publish-template`, `migrate-workspace`, `update-self`, `update-published-template`: `<TASK_FILE_GLOB>` substitution replaced by the exact path; worker-side parse instructions updated.
- `.agents/skills/launch-task/references/worker-failure.md` and `dead-worker-recovery.md`: the harden-specific report path example and the hardcoded `/home/user/worktrees/` path, which is wrong for a nested worker; point at the worker's work dir from `mngr list`.
- `.agents/shared/references/harden-contention.md`: unchanged.
- `docs/system/workspace-internals.md`: the worker provisioning description.

### Eval configs (in `/Users/markally/imbue/wt-mngr-sub-worker-eval/apps/minds_evals/configs/`)

- `eval-config-todo-app-harden-direct.json` and `eval-config-todo-app-harden-parallel.json`: copies of the existing harden config with `dwt_branch` pinned to the two arm branches; `timeout_seconds` 9000 as today.

## Implementation phases

1. **Level-agnostic contract, behavior unchanged.** Exact `task_file` stamping and parser; the `lead_agent` label; `clobber` on the runtime sync; delete the install script, its test, the template line, and the gitignore rule; harden task bodies point at the worker SKILL.md; every dispatcher's task body drops the glob. The contract tests are updated in the same change. A direct harden pass behaves identically. Ends with a passing root suite.
2. **Reporting and nested primitives.** The `report` subcommand with fallback; `await` archiving; child-aware idle detection; `question` universal; the prose dependents updated to a working state.
3. **Nested proof.** The live release test: two fixture task bodies (outer launches inner, inner asks a `question`, both write markers, commit, report `done`) run in an isolated mngr host dir against a throwaway clone of this repo. Run it in a workspace and fix what it finds.
4. **Parallel harden pass and the eval.** The parallel paragraph in `harden-creation.md`; the two arm branches; push them; smoke (probe, one `direct` trial reviewed by hand); then the two-arm, three-attempt comparison from the eval worktree.
5. **Prose rewrite.** A human pass over every prose dependent once the branch is complete and tested.

## Testing strategy

- **Unit and contract (every PR, no mngr):** the launcher's argv lifecycle through the recording runner and the live-CLI validator, including the nested paths (launch under `MNGR_AGENT_NAME=<worker>` stamps and labels that worker as lead; the idle check with children in each state; `report` push and fallback); the parser; the dispatch contract test executing every dispatching skill's real task-file block and launching on the result; template stacking. Ratchets and `test_skill_mngr_references.py` keep prose commands honest.
- **Live (release, mngr + tmux + credentials):** `test_nested_dispatch_live.py`, in the shape of the chat message-conservation release test: isolated `MNGR_HOST_DIR` profile, isolated tmux server, work repo under gitignored `.test_output/`, skipped when mngr, claude, or credentials are absent. It launches an outer claude worker whose task body launches an inner one; the inner emits a `question` gate, the outer answers it, both write markers naming themselves and their `lead_agent`, commit, and report `done`. Assertions: both markers on the outer branch after the outer merges the inner; each level's report archived exactly once; the top-level `await` (driven with a short poll interval) never returned 76 while the inner worker was alive; the inner worker's `lead_agent` label names the outer.
- **Evals (manual, from the eval worktree):** probe first; one `direct` trial reviewed by hand (worker transcripts, reports, harness failure signatures, wall-clock); then `direct` and `parallel` at three attempts each. Pass: gates 1.0 on every attempt, zero worker harness failure signatures, `parallel` mean wall-clock below `direct`.
- **Edge cases covered explicitly:** a task file with no `lead_agent` (report fallback via the label); a stale `report.md` at launch; a report landing between two idle polls; a child shed by OOM while its parent waits (the shed check runs before the idle check at every level); a sibling that reports `stuck`; a conflicted sibling merge; `launch` run from a subdirectory of the repo.

## Open questions

- **Provisioning overhead versus the parallel gain.** Each sibling pays a venv converge and plugin install. On an app as small as the todo app that may eat most of the wall-clock saved; only the eval answers this, and a negative result would argue for parallelism only above some creation size.
- **Sibling questions under the eval's polling prompt.** The eval's last turn tells the chat agent not to message the user until hardening finishes. A sibling `question` that the parallel lead re-raises reaches the chat agent, which by `lead-proxy.md` answers implementation questions itself and escalates user-intent ones. Whether any interface question in the todo app reads as user intent, and what the chat agent does with it under that prompt, is unknown until the smoke run.
