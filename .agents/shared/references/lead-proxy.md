# Lead-side proxy flow

How a lead drives its worker to completion and brings the worker's gate and
status reports to the user. The skill that sent you here supplies the
substitutions: the worker's name, its branch, the task file, the reports
directory, and which gate names and terminal statuses apply.

**None of this machinery reaches the user.** Leases, polls, `await`, milestones,
merges, review gates and worker names are how the work gets done, not what got
done, so they belong in no chat message and no progress step -- "Fresh, no lease.
Merging the milestone" names plumbing nobody can see. Say what changed, or nothing.

You follow this file for as long as the worker runs: after a compaction,
re-read it before you handle the next report.

## Contents

Every lead reads the sections marked "every pass". The others open with the
condition that sends you to them, and four of them hand off to a file of their
own. Those files, and every other reference this one names without a
directory, sit beside it.

- Polling for the next report (every pass)
- Diagnose worker liveness before invoking failure flow (a poll that timed out
  or exited 76; detail in `lead-proxy-liveness.md`)
- Do not interrupt more recent user work (every pass)
- Parsing the report (every pass)
- Deciding: answer gate yourself vs. escalate (on `type: gate`)
- Milestone reports: provisional merge (on `type: milestone`; detail in
  `lead-proxy-milestones.md`)
- Terminal status: act and stop polling (on `type: status`)
- When you are a worker yourself (a worker that launched its own worker; detail
  in `lead-proxy-intermediate-lead.md`)
- `mngr rsync` rationale (debugging the launcher's transfer; detail in
  `lead-proxy-rsync.md`)

## Polling for the next report

Poll for the report file with `create_worker.py await`, started through
`system/scripts/workspace_bare_scripts/run_in_background.py`:

```bash
python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Wait for the background agent" --waiter -- \
    uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py await \
    --name <WORKER_NAME> \
    --task-file <TASK_FILE>
```

`await` reads `finish_report_path` from the task file's frontmatter, blocks
until that file appears, prints its contents, and exits 0.
`run_in_background.py` returns at once and runs it detached; when `await`
exits, its exit code and everything it printed arrive in your chat as a
message, and that message starts your next turn. Use it whatever your harness:
most harnesses' own background tools do not wake you, and this works on every
one. `--name <WORKER_NAME>` is required (the same name you passed to
`launch`) so the poll also watches the OOM shed ledger. `--timeout` defaults to
`30m`; pass e.g. `--timeout 60m` to re-arm with a longer wait.

The message is a `<background-task-report>`: its `Exit code:` line is
`await`'s, and its `<output>` is what `await` printed -- the report contents,
YAML frontmatter (`type`, `name`) plus a body. The chat shows the user only its
one-line summary. Read the exit code first:

- **0** -- a report was printed and archived. Go to "Parsing the report". A
  milestone file under `<REPORTS_DIR>/milestones/` comes back the same way
  ("Milestone reports: provisional merge").
- **124** -- the timeout tripped with no report. Do *not* treat it as a
  terminal failure yet: see "Diagnose worker liveness before invoking failure
  flow".
- **76** -- `await` watched the worker end its turn with no report and nothing
  pending. Diagnose it the same way, at once rather than after the timeout.
- **75** -- the worker's own agent was **shed by the OOM daemon** to relieve
  memory pressure: it will not report until revived. This is not a worker bug.
  Free memory with the user per `freeing-memory.md` first, since a revival
  into the same pressure is shed again, then revive the
  worker by nudging it to continue with `create_worker.py reply --task-file
  <TASK_FILE> -m continue`, whose send relaunches a shed agent. You do not need
  to resend the task: it survives in the worker's conversation history, and a
  SessionStart hook already tells the revived worker it was paused, so it
  re-checks state before continuing. Free memory the same way whenever a
  worker's `question` gate reports that a test command was shed: a rerun into
  the same pressure is shed again.

`await` is a generic poll-until-file primitive; the gate cycle below is this
flow's *use* of it. A caller that launches one tightly scoped agent and waits
for one finish report, with no gate handling, uses the same `await`, or the
synchronous `create_worker.py launch-sync` wrapper.

### Never sleep on a worker

Once the poll is armed, **end your turn**: its message wakes you with the
report in seconds, while a `sleep N` is a guess at someone else's finishing time
and every second between the report landing and the sleep expiring is dead time
on the critical path. Ending your turn is safe -- while the poll runs, your
chat is busy (the chat shows it waiting on a background task, not done), and a
worker that is busy, or has a live worker of its own, never counts as idle,
so the liveness check will not mistake you for a wedged one.

**A worker's report is never polled.** Sleeping on a `find` over its reports
directory, on `mngr list`, or on `tmux capture-pane` against its pane is the
same guess in a different command; an armed `await` plus ending the turn is the
only sanctioned wait on a worker.

With several workers out, arm one poll per worker before ending the turn. Each
poll's message wakes you separately, so you act on whichever reports first and
merge it while the others are still running.

## Diagnose worker liveness before invoking failure flow

A poll that timed out (exit 124) or exited 76 has not shown a failure. Long
stages (the test gate, a large implementation) can legitimately exceed 30
minutes on a healthy worker, and a worker whose session died is a crash to
recover, not a failure to surface. Before invoking the failure flow, read
`lead-proxy-liveness.md` and follow it: it has the check of the worker's
session, what counts as alive, the recovery of a dead worker, and what exit 76
does and does not mean.

## Do not interrupt more recent user work

If the user gave you a more recent task since launching the worker, finish that
task first. The report's message is informational -- act on it once the
user's current request is complete.

## Parsing the report

Parse the YAML frontmatter: `type` (`gate`, `milestone`, or `status`) and `name`
(skill-specific). The body is the message the user needs to see. A `milestone`
report also carries `commit:` and `branch:`.

If the file does not parse (no frontmatter, unknown type, truncated), treat it
as a terminal failure.

You never move a report by hand: the `await` that printed it already archived
it into `<REPORTS_DIR>/consumed/` (timestamped, named by its `type` and `name`;
a milestone keeps its own file name), so `finish_report_path` is clear for the
worker's next report and the reports directory is clean for the next pass.

## Deciding: answer gate yourself vs. escalate

On `type: gate`:

- **Answer yourself** for implementation details: script structure, naming
  conventions, which utility to reuse, file layout, agentskills.io compliance,
  or anything you can determine from reading files or applying your skill's own
  guidelines.
- **Escalate to the user** for user intent, scope, subjective preference, or
  domain knowledge you do not have. `final-creation` gates always escalate.
  `outline-approval` gates default to answer-yourself; only escalate if the
  worker has surfaced a *genuine process question* (a decision about user
  intent, scope, or domain that you cannot make from context). Most
  outline gates do not contain such questions and should not be forwarded. **One
  exception: an outline that carries a cost/time estimate the user will incur (a
  metered pipeline, a paid API) is a spend decision -- surface that estimate and
  get their approval rather than auto-answering the gate.**
- **Mix**: if a gate bundles an approval (escalate) with implementation
  sub-questions, pre-answer the sub-questions in the message you forward to the
  user so they do not have to weigh in on them.

The worker is framed as addressing the user directly. When you answer, write
your reply in the user's voice and forward it to the worker's chat:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py reply \
    --task-file <TASK_FILE> -m "<reply, in the user's voice>"
```

`reply` addresses the worker by the agent id `launch` stamped into the task
file's frontmatter (`worker_agent_id`) and sends through the chat app
(`system/scripts/message_chat.py`, which falls back to `mngr message` on its
own when the chat app cannot take the message); never message a worker by its
mngr name.

To escalate, ask the user, wait for the user's reply, then forward it the same
way.

After forwarding, re-arm the background poll and end your turn.

## Milestone reports: provisional merge

On `type: milestone` the worker is not asking for anything: it committed
something usable and carried on. The default is to merge the commit the report
pins, make it usable, tell the user in one line, and re-arm the poll; the
worker's gates and terminal status are still coming. Read
`lead-proxy-milestones.md` when a milestone report arrives: it has when not to
merge, the checks before the merge, the merge itself, and the provisional
go-live. Its "Rolling back a provisional merge" is what you follow when the
user rejects a milestone you merged.

## Terminal status: act and stop polling

On `type: status`:

- `name: done` -- read its `Undeclared couplings:` and `Flaky tests:` lines
  (and, for an app's crystallize pass, its `Phone:` and `Sharing:` lines)
  from its body, or, when that body is only "Committed on branch" or
  "Verified on branch", from the worker's last `final-creation` gate (under
  `<REPORTS_DIR>/consumed/`), which carries them instead. If it lists
  `Undeclared couplings:`, they are built-in defects (a built-in suite
  observes a path nothing declares, so the test selector missed it): add them
  to the pass's single report per `report-built-in-issues.md`. If it lists
  `Flaky tests:`, the worker left them unfixed: a test of a built-in suite is
  a built-in defect and goes in that same report; a test of a creation the
  workspace built goes in a regular ticket (`tk create`), so it is fixed later
  rather than lost with the report. If its `Phone:` or `Sharing:` line names
  something the worker could not fix, that goes in a regular ticket the same
  way.
  Then merge the worker's branch:
  ```bash
  git merge --no-ff <WORKER_BRANCH>
  ```
  No fetch is needed first: the worker runs in a linked worktree of this same
  repository, so its branch already exists in the shared ref store (and a
  `git fetch . <WORKER_BRANCH>:<WORKER_BRANCH>` would be refused anyway while
  the worker's worktree has the branch checked out).
  Any commits you already provisionally merged from this branch (see "Milestone
  reports: provisional merge") are ancestors of HEAD, so this merge brings only
  the remainder and the freshness check covers exactly the window since that
  provisional merge.
  If the merge changed a `pyproject.toml` or `uv.lock` (a new skill's `python/`
  project, a dependency added to a skill or an app), bring the venv up to date
  before running anything from it; `uv run --no-sync` never installs:

  ```bash
  git diff --quiet HEAD^1 HEAD -- '*pyproject.toml' uv.lock || uv sync --all-packages --frozen
  ```
  On a clean merge, destroy the worker before your skill's go-live, and close
  any tracking ticket where your skill says to (at once, when it defines no
  go-live):
  ```bash
  uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py destroy --name <WORKER_NAME>
  ```
  Destroy takes the worker's own workers with it and keeps what you may
  still need: the branch `mngr/<WORKER_NAME>`, the transcript (under
  `$MNGR_HOST_DIR/preserved/`), and the runtime dir of every worker it
  launched, relocated into your tree at the same paths. It prints one outcome
  line per agent and exits non-zero if any could not be destroyed; report that
  rather than retrying blindly.
  On a conflict, recovery depends on your skill: if it defines a staleness rule
  (the harden skills do, in the harden-contention reference),
  abort the merge and follow that rule -- the worker catches its branch up and
  resolves the conflict there -- rather than resolving it yourself; otherwise
  resolve the conflict manually.

- `name: stuck`, or the 30m timeout tripped without a report arriving -- follow
  `.agents/skills/launch-task/references/worker-failure.md`: surface the report
  body (or its absence) to the user, point at the branch and worker agent, then
  stop the worker (and its own workers) so no process stays behind:
  ```bash
  uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py stop --name <WORKER_NAME>
  ```
  Its branch, worktree, and transcript stay for inspection. A timeout is
  the same once the liveness diagnosis says the worker is wedged, or dead
  with a recovery that did not bring it back.

- `name: no-update-needed` (or other skill-specific benign no-op terminals) --
  the worker decided there was nothing to do. Close any tracking ticket and
  destroy the worker exactly as on `done`, with nothing to merge; do not
  invoke the failure flow. Optionally surface the one-sentence reason to the
  user.

In every status case the report is already in `<REPORTS_DIR>/consumed/`, with
nothing for you to move.

### Resuming after the user overrides a failure

When the user answers a `stuck` report with a decision (apply it anyway, an
answer to what blocked the worker), resume the same worker rather than
finishing its task yourself. Re-take whatever preconditions your skill holds
for a pass (a lease, a run record), then send it the decision:

```bash
uv run --no-sync .agents/skills/launch-task/scripts/create_worker.py reply \
    --task-file <TASK_FILE> -m "<the user's decision>"
```

The send brings the stopped worker back in its worktree with its conversation
intact, and `reply` blanks the `archived_at` label the stop set, so a later
crash of it is restarted like any other
(`.agents/skills/launch-task/references/dead-worker-recovery.md`). Then re-arm
the `await` poll and end your turn.

## When you are a worker yourself

Everything above holds unchanged when you are an intermediate lead: a worker
that launched its own worker. Four rules are yours alone, about answering your
worker's `question`, merging one level, destroying or stopping it, and how long
you await it. Read `lead-proxy-intermediate-lead.md` before you launch a
worker from a worker.

## `mngr rsync` rationale

The launcher copies the runtime directory to the worker itself at `launch`, at
whatever level of the dispatch is launching, so no lead runs `mngr rsync` by
hand. Read `lead-proxy-rsync.md` when you are debugging that transfer, or
writing a sync of your own.
