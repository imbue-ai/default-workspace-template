# Worker milestones: let the lead use in-flight work before `done`

Design rationale and alternatives: `proposal.md` beside this file.

## Overview

- A harden worker has a usable creation on its branch by `op-crystallize`
  Stage 3, but the lead cannot touch it until the terminal `done` because the
  only signals a worker emits are blocking gates and terminal statuses.
- This spec adds a third, non-blocking report kind, the **milestone**: a
  free-form name the worker attaches to a specific commit, with a record of
  what was tested at that commit. The lead's existing poll returns it; the lead
  may **provisionally merge** that exact commit and start using the creation
  while the worker continues hardening.
- Milestone names are chosen by the worker per task. No enum exists in prose
  or code; guidance says *when* a milestone is worth declaring, never *which*.
- `done` handling is unchanged. A provisional merge advances the merge-base,
  so the `done` merge brings only the remainder and the existing freshness
  rule in `harden-contention.md` covers exactly the post-milestone window.
- Scope of the POC: the shared reporting/proxy references and
  `create_worker.py` gain the mechanism (so every worker flow can use it);
  only `op-crystallize` and `crystallize-creation` get milestone guidance.
  `update` / `heal` guidance is a follow-up.

## Expected behavior

### Worker side

- A worker declares a milestone by (1) committing, (2) writing
  `<RUNTIME_REPORTS_DIR>/milestones/<sha7>-<name>.md`, (3) syncing the reports
  directory to the lead exactly as for a gate (same `mngr rsync`, same
  same-repo fallback), and (4) **continuing its turn**. It never stops to wait
  for the lead.
- `<name>` is a kebab-case slug (`[a-z0-9]+(-[a-z0-9]+)*`) the worker chooses to
  describe what is true at that commit. `<sha7>` is the first seven characters
  of the commit sha, so the same name declared again at a later commit is a new
  file and a new event.
- The file's frontmatter is `type: milestone`, `name: <name>`, `commit: <full
  sha>`, `branch: <worker branch>`. The body, addressed to the user, has three
  parts: what is usable now and how to use it; a required `## Tested` section
  naming the exact commands/suites/scenarios/gates run at this commit with
  their result and, explicitly, what has not been run; and a `## Still pending`
  section listing what remains before `done`.
- A milestone whose delivery fails is best-effort: the worker continues, and
  `done` still carries everything.
- In `op-crystallize` the milestone comes at different points per shape.
  Pre-existing shape (app): at the end of Stage 3, once the app's tests pass on
  the branch. Reconstruct shape (skill): at the end of Stage 4, once the
  scenarios pass and `validate_skill.py` prints `ok` -- not earlier, because at
  Stage 3 the user has approved only an outline and a skill whose scenarios
  have never run is not worth their time. Further milestones are at the
  worker's discretion.

### Lead side

- `create_worker.py await` returns 0 and prints a milestone file exactly as it
  prints `report.md`, whenever a file in `milestones/` has no same-named entry
  in `consumed/`. `report.md` takes precedence on any poll where both exist.
  await also prints the milestone file's path on stderr so the lead can consume
  it.
- On `type: milestone` the lead reads the body (usable / tested / pending) and
  **defaults to merging**. It does not merge when the body says the creation
  is not yet runnable, when a pre-merge check fails, or while the user's more
  recent request is still in progress (handle it afterwards). Either way it
  consumes the file (`mv` to `consumed/`) and re-arms the poll; the consumed
  copy still carries the sha, so a deferred milestone can be merged later.
- The provisional merge is `git merge --no-ff <commit> -m "Provisional merge
  of <worker> at milestone <name>"` -- the pinned sha, never the branch tip --
  behind the same three pre-merge checks `harden-contention.md` requires for
  `done` (foreground lease, freshness over the creation's footprint, abort on
  conflict). The lead does not re-run checks `## Tested` names as passed at
  this commit; it may run ones the worker has not.
- Provisional go-live is the minimum needed to use the creation: a skill is on
  disk at `.agents/skills/<name>/` and invocable; an app or service gets its
  tab refreshed. The full go-live (post-crystallize migration, ticket close)
  still waits for `done`. The lead tells the user in one line what is usable
  and what is still pending.
- Rollback is `git revert -m 1 <provisional-merge-commit>`. A revert is a
  rejection of that milestone: the lead messages the worker with why. Because
  the reverted commits remain ancestors of HEAD, a later merge from the same
  branch would silently omit them, so before any later merge from that branch
  the lead first reinstates with `git revert <revert-commit>` or supersedes
  the pass per `harden-contention.md`.
- On `done` the lead runs today's flow unchanged. Already-merged commits are
  ancestors; the merge brings the rest, and the freshness check covers the
  window since the provisional merge. A foreground edit to the creation after
  the provisional merge makes the pass stale and it is superseded as today.
- `create_worker.py launch` refuses to start while an unconsumed milestone
  exists, for the same reason it refuses a leftover `report.md`.
- `create_worker.py launch-sync` ignores milestones; its callers want one
  terminal result.
- Pull-mode detection is `ls <REPORTS_DIR>/milestones/` vs `consumed/`; no
  subcommand is added.

## Changes

### `.agents/skills/launch-task/scripts/create_worker.py`

- Add `_milestones_dir(report_path)` (`report_path.parent / "milestones"`) and
  `_unconsumed_milestones(report_path) -> list[Path]`: `milestones/*.md` whose
  basename is absent from `report_path.parent / "consumed"`, oldest first by
  mtime.
- `await_report(...)` gains `watch_milestones: bool = True`. Each loop, after
  the `report_path.is_file()` check and before the shed/idle checks, if
  `watch_milestones` and an unconsumed milestone exists: write its contents to
  `out`, print `create_worker: milestone report at <path>; move it to
  <consumed_dir>/ once handled` to stderr, return 0.
- `launch(...)`: after the stale-report guard, refuse (exit 2) when
  `_unconsumed_milestones(report_path)` is non-empty, with the same "confirm it
  was handled and move it aside" wording.
- `launch_sync(...)`: call `await_report` with `watch_milestones=False`.
- Module docstring: describe the milestone watch under `await`.

### `.agents/skills/launch-task/scripts/create_worker_test.py`

- `await` returns a milestone when no report exists; prints its path to
  stderr.
- `report.md` wins when both exist on the same poll.
- A milestone whose basename is in `consumed/` is skipped (await keeps
  polling / times out).
- `watch_milestones=False` ignores an unconsumed milestone.
- `launch` refuses on an unconsumed milestone and proceeds once it is moved to
  `consumed/`.
- `launch_sync` collects the terminal report even when a milestone is present.
- A git-backed test (tmp repo, lead branch plus a linked worktree for the
  worker): worker commits A and declares a milestone at A; lead provisionally
  merges A by sha; worker commits B; lead merges the branch as `done`; assert B
  is in the lead's history exactly once and that `git diff --name-only
  $(git merge-base HEAD <branch>) HEAD -- <creation path>` is empty. Also
  assert the revert rule: after `git revert -m 1` of the provisional merge, a
  plain `done` merge does not contain A's change, and reverting the revert
  first restores it.

### `.agents/shared/references/worker-reporting.md`

- New section "Milestone reports (non-blocking)" after "Reporting procedure":
  when to use (a commit the lead could start using before `done`), the four
  steps (commit; write `milestones/<sha7>-<name>.md`; sync the reports dir the
  same way as step 2, fallback included; continue without stopping), the name
  rule (a slug the worker chooses; no fixed list), the frontmatter and the
  body template with `## Tested` (required: exact commands/suites/scenarios/
  gates run and their result, plus what has not been run) and `## Still
  pending`. State that a failed delivery is best-effort.
- "Task-file inputs" / step 3: note that only gates and terminal statuses stop
  the turn; milestones do not.

### `.agents/shared/references/lead-proxy.md`

- "Polling for the next report": await also returns milestone files (and
  prints their path on stderr); `report.md` has precedence.
- New section "Milestone reports: provisional merge" between the gate and
  terminal sections: read usable/tested/pending; the default-merge rule and
  the three do-not-merge cases; the merge command with the pinned sha; the
  pre-merge checks by reference to `harden-contention.md`; skip re-running
  checks `## Tested` names as passed at this commit; provisional go-live per
  creation; the one-line user message; consume (`mv` to `consumed/`) whether or
  not merged; re-arm.
- Same section, "Rolling back": `git revert -m 1 <merge-commit>`, message the
  worker, and the reinstate-before-any-later-merge rule.
- "Terminal status: act and stop polling", `done`: note that provisionally
  merged commits are ancestors and the merge brings the remainder.

### `.agents/shared/references/harden-contention.md`

- "Before merge": state that a provisional milestone merge runs the same three
  checks, that it is the one sanctioned way not-yet-hardened work reaches the
  lead's branch (labelled as such in the merge commit, verified only as far as
  the milestone's `## Tested` states), and that the freshness rule composes
  because the merge-base advances to the milestone commit.
- "Superseding a stale pass": note that a provisionally merged milestone
  survives on the lead's branch when the worker branch is deleted; and that a
  reverted milestone must be reinstated or superseded before any later merge
  from that branch.

### `.agents/shared/worker/references/op-crystallize.md`

- "Valid report `name:` values": add "Milestones: `type: milestone`, any
  slug you choose (see `worker-reporting.md`); non-blocking."
- Stage 3: add a closing paragraph -- pre-existing shape (app): once the
  app's tests pass on your branch, commit and declare a milestone named for
  what is true at that point; its `## Tested` must list exactly what you ran.
  Reconstruct shape (skill): no milestone yet; it comes at the end of Stage 4.
- Stage 4: add a closing paragraph -- reconstruct shape (skill): once the
  scenarios pass and `validate_skill.py` prints `ok`, commit and declare the
  milestone (the review gates are still ahead and `## Tested` says so).
  Further milestones are at your discretion.

### `.agents/skills/crystallize-creation/SKILL.md`

- Step 3 task-file body: add a short `## Milestones` paragraph telling the
  worker the lead will merge the first usable version as soon as it declares a
  milestone, and to name it for what is true at that commit.
- Step 5 substitutions: add "Milestones: any name, non-blocking -> provisional
  merge per `lead-proxy.md`; provisional go-live: skill usable at
  `.agents/skills/$NAME/`; app -> refresh the tab. Step 6 still runs only on
  `done`."
- Step 6: one sentence that a provisional merge does not change Step 6; the
  `done` merge brings the remainder.

### `.agents/skills/launch-task/SKILL.md`

- Task-file template "Valid `name:` values": add "`milestone` reports
  (`type: milestone`, any name; non-blocking, see `worker-reporting.md`)".
- Step 4 substitutions: add the milestone line pointing at `lead-proxy.md`.

### `docs/system/specs/worker-partial-artifacts/`

- `proposal.md` (already written) and this `concise.md`.

## Verification

- `cd .agents/skills/launch-task/scripts && uv run pytest create_worker_test.py`
  (or the root invocation the project uses) -- all pass, including the new
  milestone and git-backed tests; the relevant ratchets stay at or below their
  counts.
- Read-through of the prose path: `worker-reporting.md` -> `op-crystallize.md`
  (worker declares) -> `lead-proxy.md` -> `harden-contention.md` ->
  `crystallize-creation/SKILL.md` (lead merges, then `done`) reads coherently
  with no dangling reference.
- `uv run .agents/shared/scripts/validate_skill.py` passes for
  `crystallize-creation` and `launch-task`.
- Grep for any fixed milestone name list (`code-complete`, `initial-creation`)
  in `.agents/` returns only example wording, never an enum or a validator.
- The full `mngr create` round trip (worker declares a milestone, lead merges
  it, user runs the skill, `done` merges the rest) runs only inside a
  workspace container and is a manual follow-up.
