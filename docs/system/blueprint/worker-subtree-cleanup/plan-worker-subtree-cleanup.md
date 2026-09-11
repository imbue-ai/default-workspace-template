# Worker subtree cleanup: destroy after merge, stop on failure, nothing left dangling

## Overview

- The nested-dispatch contract leaves every worker it creates in place forever. Sub-workers are stopped but never destroyed, top-level workers are never even stopped, and no path that destroys a lead looks for that lead's children. Names, worktrees (each with a full venv), branches, and live claude processes accumulate with nothing to reclaim them.
- The "stop, never destroy" rule was written for one reason: the eval capture reads a sub-worker's reports from its *lead's* work dir. That constraint is narrower than the rule. mngr already copies a destroyed agent's transcript to `preserved/`, so destroying a sub-worker loses nothing the eval reads; only an intermediate lead's work dir has to outlive capture, and only for the reports pushed into it.
- The fix keeps the dispatch level-agnostic and moves the whole lifecycle into the launcher: `launch` labels each worker with its runtime dir, `destroy` becomes recursive and relocates every descendant's runtime dir into the destroying lead's tree before tearing the subtree down, and a new `stop` subcommand replaces the raw `mngr stop` in prose. Leads then follow one rule at every level: **destroy after merge, stop on failure, never leave a process running for a worker you are done with.**
- Nothing sweeps in the background. Reclamation is inline on the lifecycle paths (merge, supersede, abandoned takeover, `launch-sync`), and a worker stopped after a failure stays, name and worktree included, until a later pass supersedes it or the user asks for cleanup.
- Out of scope: the eval-side attribution of a grandchild's relocated reports (minds_evals is changing separately), the milestone delivery subcommand (`/tmp/milestone-report-subcommand.md`), and the cross-version flows (`update-self`, `publish-template`, `update-published-template`), which pin the launcher's floor interface and hand-roll their own commands.

## Expected behavior

**Launch**

- `launch` stamps two labels on every worker: `lead_agent=<lead>` (as today) and `runtime_dir=<repo-root-relative runtime dir>`. `mngr list` alone now shows the whole dispatch tree and where each node's task file and reports live.
- When `mngr create` fails and `mngr list` shows an agent of the requested name, the failure message names that agent's state and `lead_agent` label and says to run `create_worker.py destroy --name <name>` if it is finished with, or pick another name. A name collision after a stopped failure is an expected event, not a mystery.

**Destroy**

- `create_worker.py destroy --name <worker>` is recursive by default. It resolves the subtree from one `mngr list` (every agent whose `lead_agent` chain reaches the worker) and works deepest-first: before each agent is destroyed, the worker itself last, the runtime dirs of the workers *it* dispatched are pulled out of its worktree into the caller's tree at the same repo-relative paths (its `data/.tasks/` tree, minus its own runtime dir, which the caller already has and whose worker-side copy carries the worker's own `report.md`). Pulling by convention rather than by label is what carries the runtime dir of a sub-worker its lead had already destroyed: that lead's worktree holds the only copy and no record is left to find it by. Relocation merges into any existing directory, newest file wins.
- Branches survive by default. `--delete-branches` deletes `mngr/<name>` for every agent in the subtree; the supersede and abandoned-takeover paths pass it because nothing on a superseded pass is trustworthy.
- Before destroying each agent, `destroy` prints that agent's commits not reachable from HEAD and whether its worktree is dirty, then proceeds. There is no refusal flag: committed work stays on the branch, and the dirty case is what dead-worker-recovery's salvage flow is for.
- RUNNING descendants are destroyed too. A superseded or abandoned pass's in-flight siblings are exactly what needs to go.
- A failed relocation warns and the destroy continues. A failed `mngr destroy` on one agent does not stop the rest: `destroy` reports one line per agent with its outcome and exits non-zero if any agent failed.
- `--no-recursive` destroys the one agent and prints a warning naming each child left behind and its state.
- Transcripts of destroyed workers are readable at `$MNGR_HOST_DIR/preserved/<name>--<id>/events/*/common_transcript/events.jsonl`; the eval's worker capture already reads them from there.

**Stop**

- `create_worker.py stop --name <worker>` stops the worker and, by default, its whole subtree (children first) with `mngr stop --archive`. Worktrees and branches stay; only processes go. `--no-recursive` stops the one agent.
- The `archived_at` label a stop leaves behind distinguishes "stopped by its lead on purpose" from "crashed": dead-worker-recovery's restart-a-STOPPED-worker advice applies to the unlabelled ones.
- A stopped worker keeps its name. A later `launch` of the same name fails with the collision message above until someone destroys it.

**What leads do, at every level**

- `done`: run the merge-time checks, merge, then `create_worker.py destroy --name <worker>` immediately, before the calling skill's go-live. A worker that is itself a lead has already destroyed its own merged sub-workers, so the top-level destroy finds at most stopped failures underneath and relocates their runtime dirs on the way.
- `no-update-needed` and other benign no-op terminals: destroy, same as `done`, with nothing to merge.
- `stuck`: capture context per the failure flow, tell the user, then `create_worker.py stop --name <worker>`. Branch, worktree, and transcript stay for inspection; the process and its subtree's processes go.
- Timeout without a report, worker judged dead or wedged: same as `stuck` once liveness diagnosis is done.
- A stopped failure is destroyed only by a later supersede of that creation's pass (`destroy --delete-branches` on the old worker) or when the user asks.
- Idle detection is unchanged: a STOPPED child, archived or not, never counts as live, so a lead that stopped its stuck sibling reads as idle once its own turn ends, and a lead that destroyed its merged siblings has no children at all.

**Splitting a pass across siblings**

- A worker that splits its pass names each sibling with its own worker name as the prefix (`update-todo-backend`, `update-todo-frontend`). A re-run of the same pass reaches the same names only after supersede has destroyed the old subtree.
- Merged siblings are destroyed; a stuck sibling is stopped; the harden lead reports `done` or `stuck` upward exactly as before.

**Supersede, abandoned takeover, salvage, launch-sync**

- Supersede and abandoned takeover run `create_worker.py destroy --name <worker> --delete-branches`; the separate `git branch -D` line goes away.
- The salvage flow commits the WIP, then runs `create_worker.py destroy --name <worker>` (branch kept, subtree handled); its claim that `--no-allow-worktree-removal` keeps the branch alive is corrected.
- `launch-sync` destroys recursively after collecting the report; `--keep-agent` still leaves the worker untouched.

## Implementation plan

### Launcher: `.agents/skills/launch-task/scripts/create_worker.py`

- `_repo_relative_path(path, runner)`: generalize `_repo_relative_task_path` so `launch` can relativize the runtime dir the same way it relativizes the task file; `_repo_relative_task_path` becomes a call to it.
- `launch`: append `--label runtime_dir=<_repo_relative_path(runtime_dir)>` to `create_argv` after the `lead_agent` label. On a `CalledProcessError` from `mngr create`, call `_agent_records` and `_record_named(records, name)`; when a record exists, extend the failure message with its `state`, its `lead_agent` label, and the `destroy --name` remedy.
- New label helpers next to `_record_label`: `_RUNTIME_DIR_LABEL = "runtime_dir"`, `_LEAD_AGENT_LABEL = "lead_agent"` (replace the string literals already used in `_worker_is_idle` and `_deliver_report_through_listing`).
- `_dispatch_subtree(root_name, records) -> tuple[Mapping, ...]`: every record whose `lead_agent` chain reaches `root_name`, in post-order (children before their lead), excluding the root. Cycle-safe (a visited set) and tolerant of records with no name. One `mngr list` feeds it.
- `_relocate_task_dirs(record, runner) -> bool`: reads the agent's `runtime_dir` label; when absent (its own dir could not be excluded), prints a warning and returns `False`. Otherwise runs `mngr rsync <name>:data/.tasks/ ./data/.tasks/ --uncommitted-changes=clobber -- --update --exclude=/<own dir under data/.tasks>` (the pull form of `rsync_dir`'s conventions: agent endpoint as SOURCE, `./`-prefixed local DESTINATION, trailing slashes on both) with `check=False`, warns on non-zero, returns whether it landed. Give `rsync_dir` a sibling `rsync_dir_from(name, source_dir, runner, excludes)` rather than a direction flag, so both docstrings stay plain.
- `_unmerged_work_warning(record, runner)`: `git -C <work_dir> status --porcelain` and `git rev-list --count HEAD..mngr/<name>` through the runner; prints one line naming the dirty state and the count of commits not in HEAD. Never blocks. Skipped with a note when the record has no `work_dir`.
- `class _LifecycleOutcome(NamedTuple)`: `name`, `action` (`"destroyed"` / `"stopped"`), `succeeded`, `detail`. `_print_outcomes(outcomes)` writes one line per agent to stderr and returns the exit code (`0` if all succeeded, else `1`).
- `destroy(name, runner, recursive=True, delete_branches=False) -> int` (return type changes from `None`): resolve records; subtree = `_dispatch_subtree` when recursive, else `()`. For each subtree record: `_relocate_task_dirs`, `_unmerged_work_warning`, then `mngr destroy <child> --force` plus `-b` when `delete_branches`, `check=False`, recording an outcome. Then the root: `_relocate_task_dirs` (when listed), warning, `mngr destroy <name> --force [-b]`, outcome. When not recursive and the subtree is non-empty, print the orphan warning (name and state per child) before destroying the root. Return `_print_outcomes(...)`.
- `stop(name, runner, recursive=True) -> int`: same traversal, no relocation, `mngr stop <agent> --archive` per agent (children first, root last), outcomes and exit code as `destroy`.
- `launch_sync`: `destroy(name, runner)` now returns an exit code; a non-zero destroy is reported in the result JSON as `"destroy_failed": true` and the command exits non-zero after emitting the JSON, so a service sees both the report and the leftover.
- `_run_destroy`, new `_run_stop`, `build_parser`: `destroy` gains `--no-recursive` and `--delete-branches`; new `stop` subparser with `--name` (required) and `--no-recursive`; `main` dispatches `stop`.
- Module docstring: the subcommand list gains `stop`, the `destroy` paragraph describes recursion, relocation, `--delete-branches`, and the outcome lines; the "Launch lifecycle commands" block gains the `runtime_dir` label; a new "Teardown commands" block lists the `mngr rsync` pull, `mngr destroy ... --force [-b]`, and `mngr stop ... --archive` argv shapes.
- Adjacent fix while in the file: the idle-exit message at the end of `await_report` points at `data/worktrees/<name>-*/`; worktrees live under the template's `worktree_base_folder` (`/home/user/worktrees/`), and the record's `work_dir` from `mngr list` is the exact path. Say that instead.

### Prose

- `.agents/shared/references/lead-proxy.md`
  - "Terminal status: act and stop polling": `done` becomes merge, then `create_worker.py destroy --name <WORKER_NAME>` before the calling skill's go-live, with one sentence on what destroy keeps (branch, preserved transcript, relocated sub-worker runtime dirs) and what it frees. `no-update-needed` destroys too. `stuck` and the dead-after-timeout case end with `create_worker.py stop --name <WORKER_NAME>` after the failure flow.
  - "When you are a worker yourself": "Stop, never destroy" becomes "Destroy after merge, stop on failure", with the busy-count explanation kept (a merged sibling left WAITING still reads as a live child) and the note that the launcher relocates a destroyed sibling's runtime dir into your worktree, which is what your own lead's destroy later carries upward.
- `.agents/skills/launch-task/references/worker-failure.md`: step 3 becomes capture, tell the user, then `create_worker.py stop --name <worker>`; the worker's branch, worktree, and transcript remain, its process does not. Add that a stopped failure is destroyed only by a later supersede or on the user's request.
- `.agents/skills/launch-task/references/dead-worker-recovery.md`: the opening paragraph and the restart section say the restart path is for a STOPPED worker *without* an `archived_at` label (one with the label was stopped on purpose by its lead). Salvage step 4 becomes `create_worker.py destroy --name <worker>`, and the sentence about `--no-allow-worktree-removal` keeping the branch alive is replaced by the fact that destroy keeps the branch unless `--delete-branches` is passed. Keep the `mngr list` work-dir lookup.
- `.agents/shared/references/harden-contention.md`: the superseding snippet becomes the single `create_worker.py destroy --name <worker-name> --delete-branches` line plus `tk close`; the "Abandoned" bullet says "destroy it with the launcher (`--delete-branches`)" instead of "destroy the worker, delete its branch".
- `.agents/shared/worker/references/harden-creation.md`, "Splitting the pass across sub-workers": the sibling naming rule (prefix with your own worker name), and "stop a finished sibling rather than destroy it" becomes "destroy a merged sibling, stop a stuck one".
- `.agents/skills/launch-task/SKILL.md`: Step 4's `done` substitution says merge then destroy; the guideline about restarting a STOPPED worker gains the `archived_at` distinction; add one guideline on the collision message and what to do about it.
- `.agents/skills/update-system-interface/SKILL.md`: its own merge step (the one that runs harden-contention's checks) adds the destroy after a successful merge, since it does not route through `update-creation` Step 4.
- `.agents/skills/crystallize-creation/SKILL.md`, `update-creation/SKILL.md`, `heal-creation/SKILL.md`: no new commands; their `done` substitutions already defer to lead-proxy. Adjust wording where they say "merge, then Step N" so the destroy is understood to sit between.
- `.agents/changelog/mark-worker-subtree-cleanup.md`: entry in the shape of `mark-sub-worker.md`.
- `docs/system/blueprint/nested-worker-dispatch/plan-nested-worker-dispatch.md`: unchanged (historical); this plan records the reversal.

### Tests

- `.agents/skills/launch-task/scripts/create_worker_test.py` (recording runner, `_launch_argv`, `_worker_tree`, `assert_mngr_argv_valid`):
  - launch appends `--label runtime_dir=<repo-relative>` (from the root and from a subdirectory), and the argv passes the live-CLI validator.
  - collision message names the existing agent's state and lead when `mngr create` fails and the listing has the name; falls back to the current message when it does not.
  - `_dispatch_subtree`: post-order over a three-level tree, a cycle, a record without a name, an unrelated agent sharing a prefix.
  - destroy: pull-then-destroy ordering deepest-first with the root last and no pull for the root; the pull argv shape; `--delete-branches` adds `-b` to every destroy; `--no-recursive` skips children and prints the orphan warning naming each child and state; relocation failure warns and the sequence continues; a failing `mngr destroy` on one child still destroys the rest and the exit code is non-zero; unmerged-commit and dirty-tree warnings appear when the stubbed git says so.
  - stop: `mngr stop <name> --archive` per agent, children first; `--no-recursive`; partial failure semantics.
  - stuck-worker edge case: a STOPPED child carrying `archived_at` does not hold its parent busy in `_worker_is_idle`; a STOPPED sibling inside a subtree is relocated and destroyed by a recursive destroy of its lead.
  - launch_sync destroys recursively and reports `destroy_failed` on a non-zero destroy; `--keep-agent` runs neither stop nor destroy.
  - every new argv (`destroy` with flags, `stop`, the rsync pull) goes through `assert_mngr_argv_valid`.
- `.agents/skills/launch-task/scripts/dispatch_contract_test.py`: the prose-invocation sweep must reach `lead-proxy.md`, `harden-contention.md`, `worker-failure.md`, `dead-worker-recovery.md`, and `harden-creation.md` so every `create_worker.py stop|destroy ...` line in prose parses with the real parser; extend `_all_prose_launcher_invocations` to those shared references if it only scans dispatcher `SKILL.md` files today.
- `.agents/shared/scripts/test_skill_mngr_references.py`: unchanged; it keeps checking that any raw `mngr` subcommand in prose exists.
- `.agents/skills/update-self/scripts/launcher_contract_test.py`: unchanged, and update-self prose stays on the floor interface (no new flags reach it).
- `.agents/skills/launch-task/scripts/test_nested_dispatch_live.py` (release, tmux): Step 5 of the outer body runs `create_worker.py destroy --name <inner>` after the merge; the top-level body destroys the outer after merging it. Assertions: the inner is absent from `mngr list` and has a preserved transcript under the isolated host dir; after the top-level destroy, the inner's runtime dir (task file and the consumed `done` report) exists in the top-level agent's work dir at `data/.tasks/launch-task/<inner>/`; the outer's branch still carries both markers; teardown's best-effort destroys stay for the failure case.

## Implementation phases

1. **Launcher teardown primitives.** `runtime_dir` label, `_dispatch_subtree`, relocation, recursive `destroy` with `--delete-branches` and `--no-recursive`, the `stop` subcommand, outcome reporting, the collision message, `launch_sync` wiring, the idle-message path fix. Unit tests for all of it. Prose still says stop-in-place, so behavior of running flows is unchanged; the root suite passes.
2. **Prose and contract.** Every reference and skill listed above switches to destroy-after-merge and stop-on-failure; the sibling naming rule; the changelog entry. The dispatch contract test covers the shared references, and the root suite passes.
3. **Live proof.** The nested release test asserts destroy, preservation, and relocation; run it in a workspace and fix what it finds.

## Testing strategy

- **Unit and contract, every PR, no mngr:** the recording runner drives `launch`, `destroy`, `stop`, and `launch_sync` argv-by-argv, with `mngr list` output stubbed to shape the tree and `git` stubbed for the warnings; the live-CLI validator accepts every mngr argv the launcher can emit; the contract test parses every launcher invocation in prose.
- **Live (release, mngr + tmux + credentials):** the nested dispatch test, extended as above; skipped where a claude worker cannot start.
- **Manual, once, in a workspace:** dispatch a harden pass that splits into siblings, let it finish, and confirm with `mngr list` that no worker remains, that `preserved/` holds each sibling's transcript, and that the chat tree holds each sibling's runtime dir; then dispatch a second pass on the same creation and confirm the same sibling names are accepted.
- **Edge cases covered explicitly:** a subtree with a stopped stuck sibling under a merged lead; a descendant whose `runtime_dir` label is missing (older launcher); a descendant whose lead record is gone; `mngr destroy` failing on one of three agents; a dirty worktree in the subtree; `--no-recursive` on a lead with running children; a relocation target that already exists; a collision with a STOPPED, archived agent.

## Open questions

- **Eval attribution of grandchildren.** After the top-level destroy, a grandchild's reports sit in the chat agent's tree at the grandchild's own repo-relative runtime dir, but the capture keys the lookup on the grandchild's *lead's* work dir, which is gone. The fallback that makes it resolve is one line in minds_evals (an unlisted lead resolves to the chat agent's work dir). That change is being made there; this plan assumes it lands, and the relocation layout is chosen so no other eval change is needed.
- **Milestone subcommand overlap.** `/tmp/milestone-report-subcommand.md` keeps a worker's own `report.md` in its tree "for a later capture". With destroy-after-merge that copy is gone with the worktree, and the lead's `consumed/` archive is the record. Whichever change lands second should drop that sentence; nothing else overlaps.
- **Crystallize deletes its own runtime dir on `done`.** The post-crystallize migration removes `data/.tasks/harden/crystallize-<name>/`, which is where the eval reads that worker's reports. Pre-existing, and not touched here, but it means a crystallize pass's top-level reports can vanish before capture while its siblings' relocated dirs survive.
- **Branch deletion ordering inside mngr.** `mngr destroy -b` deletes the branch after its post-destroy GC removes the worktree; the live test should confirm that a subtree destroy with `--delete-branches` leaves no `mngr/*` branches behind, since a GC that skips a worktree would make the branch delete fail with a warning rather than an error.
- **Contract test reach.** Resolved during implementation: the sweep already covered `.agents/shared`, and the cross-version flows' floor-interface invocations still parse (no new flag is required). What it did not reach was a fenced block indented inside a list item, which is where the new `stop`/`destroy` lines sit; the scanner now accepts indented fences and dedents their content.
- **Live proof pending.** The nested release test skips on a machine whose installed claude differs from the version `.mngr/settings.toml` pins, so phase 3 has to run in a workspace.
