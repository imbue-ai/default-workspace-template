# Proposal: let a lead use a worker's work before the worker is done

Status: approved 2026-09-09; the spec is `concise.md` beside this file.
Branch `mark/worker-partial-artifacts`.

## The problem

A harden worker (crystallize / update / heal) has a usable creation on its
branch long before it reports `done`. In `op-crystallize` the creation is built
by Stage 3; Stages 4-6 (scenarios, review gates, final gate) are the long tail.
The Stage 3 build is frequently a large improvement over the live prototype, yet
the lead cannot touch it until the whole pass finishes, because the only signals
a worker emits are blocking gates and the terminal `done` / `stuck`.

## What changes, in one paragraph

A worker can declare a **milestone**: a free-form, task-specific name attached to
a specific commit on its branch, delivered as a non-blocking report file. The
lead's existing poll (`create_worker.py await`) returns milestones the same way
it returns gates and statuses. On a milestone the lead may **provisionally
merge** exactly that commit into its own branch and make the creation usable,
while the worker keeps going. When the worker reports `done`, the lead merges
the branch as it does today; the commits it already merged are ancestors, so
`done` brings only the remainder, and the existing freshness rule keeps working
unchanged.

## Requirements this satisfies

| # | Requirement | How |
|---|---|---|
| 1 | Workers declare a milestone before `done`; the name varies by task | A `type: milestone` report whose `name:` is any slug the worker chooses -- there is no enum anywhere, in prose or in code |
| 2 | Leads detect it and decide whether to start using it | `await` returns on a new milestone file; the lead consumes it whether or not it merges |
| 3 | Leads can merge the in-flight work at that milestone | `git merge --no-ff <commit>` of the pinned sha, behind the same pre-merge checks the `done` merge uses |
| 4 | On full completion the task is marked complete and merged as today | Unchanged `done` handling; merge-base has advanced, so only new commits land |

## Design

### 1. A milestone is a commit plus a report file

The lead can only merge a commit, so declaring a milestone means: commit, write
the milestone file, push it, and **continue without stopping the turn**. The
file lives beside the existing report, one file per milestone:

```
<reports_dir>/milestones/<sha7>-<name>.md
```

```
---
type: milestone
name: <free-form slug chosen by the worker>
commit: <full sha on the worker's branch>
branch: mngr/<worker-name>
---

<what is usable now and how to use it, addressed to the user>

## Tested
<what has been verified at this commit: the exact test commands or suites
run and their result, scenarios exercised, review gates passed -- and,
explicitly, what has NOT been run yet>

## Still pending
<what the worker will do next before `done`>
```

The `## Tested` section is required. It is for the lead: it says how much
trust the provisional build deserves, and it lets the lead skip re-running a
check the worker already ran at this exact commit (a suite it names as passed
needs no re-run before the provisional merge; an unnamed one does). The lead
records what it relied on, so a later `done` report can be compared against it.

`type: milestone` joins `gate` and `status`. The existing `parse_report` already
returns `name` as an arbitrary string, so nothing on the lead side needs a list
of allowed names.

Why a separate directory instead of `report.md`: `report.md` is a single slot
whose contract is "write, push, stop your turn" -- the lead consumes it before
the worker can write again. A milestone is non-blocking, so the worker may reach
a gate before the lead has consumed the milestone; sharing the slot would
overwrite it. One file per milestone removes the race.

Why the sha is in the filename: the worker keeps its local copy, and every later
push of the reports directory (a gate, `done`) re-delivers it. The lead skips
any milestone whose basename already exists in `consumed/`, so a re-delivered
file is inert, while the same name declared again at a new commit is a new file
and therefore a new event.

Delivery is the existing `mngr rsync` of the reports directory (and the existing
same-repo fallback). `mngr rsync` does not pass `--delete` unless asked, so the
push never disturbs the lead's `consumed/`. A milestone whose delivery fails is
best-effort: the worker continues, and `done` still carries everything.

### 2. Detection: `await` also returns milestones

`create_worker.py await` currently blocks on `finish_report_path`. It gains a
second watch: any `milestones/*.md` not present in `consumed/`. Precedence when
both exist on the same poll: `report.md` first (a gate or terminal status is the
more important event; the milestone is returned by the next re-armed poll).
Output and exit code are unchanged (print the file, exit 0), so the lead's
existing "parse frontmatter, branch on `type`" loop absorbs it.

`launch`'s stale-report guard extends to unconsumed milestones for the same
reason it exists for `report.md`. `launch-sync` (the non-interactive wrapper)
ignores milestones -- its callers want one terminal result.

Pull-mode detection needs no tooling: `ls <reports_dir>/milestones/` shows what
has arrived, `consumed/` what has been acknowledged. A `milestones` list
subcommand can come later if the prose turns out to want it.

### 3. Lead: acknowledge, then decide whether to provisionally merge

`lead-proxy.md` gets a `type: milestone` branch next to the gate and status
branches:

1. Read the body: what is usable, what `## Tested` says was verified at this
   commit, and what is still pending.
2. Decide. **Default to merging**: the point of the milestone is earlier use.
   Do not merge when the milestone body says the creation is not yet runnable
   (a design-only milestone), when the pre-merge checks below fail, or when the
   user is mid-request (the existing "do not interrupt more recent user work"
   rule applies; handle the notification afterwards).
3. Consume either way: move the file to `consumed/`. The consumed copy still
   carries the sha, so a deferred milestone can be merged later.
4. Re-arm the poll.

The provisional merge:

```bash
git merge --no-ff <commit> -m "Provisional merge of <worker> at milestone <name>"
```

Merge the **pinned sha**, never the branch tip -- the tip keeps moving and may
be mid-edit. Run the same pre-merge checks `harden-contention.md` mandates for
`done`: wait out a foreground editing lease (apps and services), run the
freshness check over the creation's footprint, and on a conflict `git merge
--abort` rather than hand-resolve. The one deliberate difference from `done`:
the merged work is **not yet hardened** -- verified only as far as `## Tested`
states. The merge commit says so, and the lead tells the user in one line what
is now usable and what is still pending. The lead does not re-run checks the
worker names as passed at this commit; it may run ones the worker has not.

Provisional go-live is the minimum needed to use the thing, not the full
go-live:

- skill: it is on disk at `.agents/skills/<name>/` and can be invoked; the
  post-crystallize migration (repointing consumers, deleting the runtime dir,
  closing the ticket) still waits for `done`.
- app / service: refresh the tab.

### 4. `done` is unchanged, and the staleness rule composes

At `done` the lead runs today's flow. Because HEAD already contains the
milestone commit, `git merge-base HEAD <worker-branch>` is the milestone commit
(or later), so:

- the `done` merge brings only the post-milestone commits, and
- the freshness check (`git diff --name-only $BASE HEAD -- <creation paths>`)
  covers exactly the window since the provisional merge.

If the user edited the creation after starting to use it, the pass is stale by
the existing rule and is superseded as today. That is the intended trade: the
foreground always wins.

### 5. Where the worker learns to declare milestones

- `worker-reporting.md` (shared): a new "Milestone reports" section -- format,
  commit-first, push, continue without stopping. This makes milestones available
  to every worker flow, including plain `launch-task` workers.
- `op-crystallize.md`: after Stage 3, "commit and declare a milestone: the
  creation is usable end to end but not yet hardened. Name it for what is true
  at that point." Further milestones (e.g. after scenarios pass) are at the
  worker's discretion. Guidance says *when*; the worker chooses the name.
- The lead's task file may add a short `## Milestones` hint ("the lead will
  merge the first usable version as soon as you declare it"). Optional; the
  worker declares milestones regardless.

## What the POC exercises

A skill crystallize. The worker reaches Stage 3, commits, declares a milestone
named for that state; the lead's poll returns it, the lead provisionally merges
the sha and tells the user the skill is usable; the user runs it while the
worker continues scenarios and review; `done` arrives, the lead merges the
remainder and runs the post-crystallize migration.

Verification for the POC:

- Unit tests on `create_worker.py`: `await` returns a milestone; `report.md` wins
  when both are present; a milestone already in `consumed/` is skipped; `launch`
  refuses on an unconsumed milestone; `launch-sync` ignores milestones.
- A scripted git walkthrough in a scratch repo (lead branch + linked worker
  worktree): commit, milestone, provisional merge by sha, more worker commits,
  `done` merge, and the freshness check reporting fresh -- proving the
  staleness rule composes.
- The full `mngr create` round trip runs only inside a workspace container and
  is a manual follow-up, as with previous specs in this area.

## Alternatives considered

| Option | Why not |
|---|---|
| Git tags (`milestone/<worker>/<name>`) on the shared ref store, no file at all | Zero transport, but a repo-global namespace to clean up, no body for "how to use it", and inconsistent with the report protocol every lead already follows |
| `type: milestone` in the single-slot `report.md` | Overwrite race once the worker proceeds to a gate before the lead consumes the milestone |
| Lead watches `git log mngr/<worker>` and merges the tip | No statement of what is usable; the tip is often mid-edit |
| A fixed milestone set per operation | Explicitly ruled out; names are task-specific |

## Risks and open questions

- **Unverified work reaches the lead's branch by design**, and the post-commit
  auto-push mirrors it to GitHub. The merge commit is labelled provisional and
  the user is told. Is that enough, or should provisional merges land on a
  side branch the lead switches to? (Proposal: enough for the POC.)
- **Rolling back a provisional merge.** `git revert -m 1 <merge-commit>` takes
  the milestone back out. One git subtlety makes this a documented rule rather
  than a free action: the reverted commits stay ancestors of HEAD, so a later
  `done` merge of the same branch would **silently omit** them. So a revert is
  a rejection of that milestone: the lead messages the worker with why (a de
  facto "no with notes"), and before any later merge from that branch it first
  reinstates with `git revert <revert-commit>`, or supersedes the pass. The
  provisional merge commit's subject names the worker and milestone so the
  revert target is easy to find.
- **Superseding after a provisional merge** deletes the worker branch as today,
  so the post-milestone hardening is redone by the new pass. Acceptable; the
  merged milestone itself survives on the lead's branch.
- **`update` and `heal` operations** get the mechanism for free through
  `worker-reporting.md`, but their op references get no milestone guidance in
  the POC. Follow-up.

## Files the spec will touch

- `.agents/shared/references/worker-reporting.md` -- milestone report section
- `.agents/shared/references/lead-proxy.md` -- milestone handling, provisional merge
- `.agents/shared/references/harden-contention.md` -- provisional merges as the one sanctioned unverified merge; note that the freshness rule composes
- `.agents/shared/worker/references/op-crystallize.md` -- Stage 3 milestone guidance
- `.agents/skills/crystallize-creation/SKILL.md` -- Step 5 report kinds and provisional go-live; optional task-file hint
- `.agents/skills/launch-task/SKILL.md` -- `milestone` listed among report kinds
- `.agents/skills/launch-task/scripts/create_worker.py` and `create_worker_test.py` -- `await` milestone watch, `launch` guard, `launch-sync` opt-out
- `docs/system/specs/worker-partial-artifacts/concise.md` -- the spec
