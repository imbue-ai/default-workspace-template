# Lead-side proxy flow

Generic mechanics for driving a worker to completion and surfacing gate/status
reports to the user. The caller supplies flow-specific substitutions (worker
name, branch, runtime path, which gate names and terminal statuses apply).

## Polling for the next report

Start a background poll for the report file with `create_worker.py await`. It
reads `finish_report_path` from the task file's frontmatter, blocks until that
file appears, prints its contents, and exits 0; on timeout it exits non-zero
(code 124). Run it with Bash's `run_in_background: true` so it returns the
instant the report lands. `--name <WORKER_NAME>` is required (the same name you
passed to `launch`) so the poll also watches the OOM shed ledger.

`await` is a generic poll-until-file primitive; the gate cycle below is this
flow's *use* of it. Non-interactive callers that launch a tightly-scoped agent
and wait for one finish report use the same `await` (or the synchronous
`create_worker.py launch-sync` wrapper) with no gate handling.

```bash
# Run with Bash run_in_background: true
uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --name <WORKER_NAME> \
    --task-file <TASK_FILE>
```

`--timeout` defaults to `30m`; pass e.g. `--timeout 60m` to re-arm with a longer
wait. The tool output is the report contents: YAML frontmatter (`type`, `name`)
plus a body. If await exits non-zero (timeout) without printing a report, do
*not* immediately treat it as a terminal failure -- see "Diagnose worker
liveness" below.

If await exits with code 75, the worker's own agent was **shed by the OOM
daemon** to relieve memory pressure: it will not report until revived. This is
not a worker bug -- revive it with `mngr start <WORKER_NAME> --restart` (a plain
`mngr message` or `mngr start` does not relaunch a shed agent), then nudge it to
continue (`mngr message <WORKER_NAME> -m continue`). You do not need to resend
the task: it survives in the worker's conversation history, and a SessionStart
hook already tells the revived worker it was paused, so it re-checks state before
continuing.

## Diagnose worker liveness before invoking failure flow

If the timeout trips without a report appearing, the worker may still be
alive and working. Long-running stages (autofix, verify-architecture, large
implementations) can legitimately exceed 30 minutes on a healthy worker.
Before invoking the failure flow, check the worker session:

```bash
tmux capture-pane -t minds-<WORKER_NAME>:claude -p -S -100 | tail -40
```

If the output shows ongoing tool use, an active spinner / "Running…" line,
or recent timestamps within the last few minutes, the worker is alive --
re-arm the poll with a longer timeout (e.g. `60m`) and continue. Only
invoke the failure flow (`.agents/skills/launch-task/references/worker-failure.md`)
if the session is dead, the agent is wedged on the same operation for an
extended period, or output has been static.

`await` makes the same call for you in the clearest case: exit code **76** means
it watched the worker's agent end its turn on several consecutive polls with no
report -- finished or wedged without reporting, worth surfacing immediately
rather than waiting out the timeout. That check never fires while the worker has
a live sub-worker of its own: an agent labelled `lead_agent=<worker>` in state
RUNNING or WAITING with no pending shed counts as the worker being busy, so an
intermediate lead waiting on its own child is never mistaken for a dead one.

## Do not interrupt more recent user work

If the user gave you a more recent task since launching the worker, finish that
task first. The report notification is informational -- act on it once the
user's current request is complete.

## Parsing the report

Parse the YAML frontmatter: `type` (`gate` or `status`) and `name`
(skill-specific). The body is the message the user needs to see.

If the file does not parse (no frontmatter, unknown type, truncated), treat it
as a terminal failure.

## Deciding: answer gate yourself vs. escalate

On `type: gate`:

- **Answer yourself** for implementation details: script structure, naming
  conventions, which utility to reuse, file layout, agentskills.io compliance,
  or anything you can determine from reading files or applying the calling
  skill's own guidelines. The user does not care about technical details --
  do not surface them.
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
your reply in the user's voice and forward via `mngr message`:

```bash
mngr message <WORKER_NAME> -m "<reply, in the user's voice>"
```

To escalate, ask the user, wait for
the user's reply, then forward it via `mngr message`.

You never move a report by hand: the `await` that printed it already archived
it into `<REPORTS_DIR>/consumed/` (timestamped, named by its `type` and `name`),
so `finish_report_path` is clear for the worker's next push. After forwarding,
re-arm the background poll.

## Terminal status: act and stop polling

On `type: status`:

- `name: done` -- merge the worker's branch:
  ```bash
  git merge --no-ff <WORKER_BRANCH>
  ```
  No fetch is needed first: the worker runs in a linked worktree of this same
  repository, so its branch already exists in the shared ref store (and a
  `git fetch . <WORKER_BRANCH>:<WORKER_BRANCH>` would be refused anyway while
  the worker's worktree has the branch checked out).
  On a clean merge, close any tracking ticket and optionally destroy the
  worker. On a conflict, recovery depends on the calling skill: if it defines
  a staleness rule (the harden flows do -- see
  `.agents/shared/references/harden-contention.md`), abort the merge and
  follow that rule rather than hand-resolving; otherwise resolve the conflict
  manually.

- `name: stuck`, or the 30m timeout tripped without a report arriving -- follow
  `.agents/skills/launch-task/references/worker-failure.md`: surface the report
  body (or its absence) to the user, point at the branch and worker agent, and
  leave both intact for manual inspection.

- `name: no-update-needed` (or other skill-specific benign no-op terminals) --
  the worker decided there was nothing to do. Close any tracking ticket and
  stop; do not merge, do not invoke the failure flow. Optionally surface the
  one-sentence reason to the user.

In every status case the report is already in `<REPORTS_DIR>/consumed/` -- the
`await` that printed it archived it there -- so the reports dir is clean for the
next run with nothing for you to move.

## When you are a worker yourself

Everything above holds unchanged when you are an intermediate lead: a worker
that launched its own worker. Four rules are yours alone.

- **A sub-worker's `question` is yours to answer first.** Answer it yourself
  whenever your own task file and the repo settle it -- that is most of them.
  Only when the answer is genuinely not available to you do you re-raise it as
  your *own* `question` gate to your lead (`create_worker.py report --type gate
  --name question`, per `.agents/shared/references/worker-reporting.md`), and
  when the reply comes back you forward it to the sub-worker **verbatim** with
  `mngr message`. Do not paraphrase a decision you did not make.
- **Merge exactly one level.** A sub-worker's branch `mngr/<sub-worker-name>`
  merges into *your* branch, with `--no-ff` and the sub-worker named in the
  merge commit message. Your own lead then sees one merged branch and never
  needs to know sub-workers existed.
- **Stop, never destroy.** Once a sub-worker's branch is merged, stop it with
  `mngr stop <sub-worker-name>` and leave it in place -- its transcript and
  pushed reports stay where a later capture can resolve them. Do not destroy
  it, and do not destroy yourself. Stopping matters for your own lead too: a
  merged sub-worker left in WAITING still reads as a live child, which keeps
  you counted as busy after you have finished.
- **Await sub-workers with `--timeout 60m`**, which fits inside the window your
  own lead is waiting out (90m for the harden flows).

## `mngr rsync` rationale

The launcher makes every transfer in this dispatch itself -- the runtime-dir
push at `launch`, and the report push a worker's `report` performs -- and they
all take this shape. Read this when you are debugging one, or writing a sync of
your own:

```bash
mngr rsync ./<SOURCE_DIR>/ <WORKER>:<DEST_DIR>/ \
    --uncommitted-changes=clobber
```

- `mngr rsync` takes `SOURCE DESTINATION` (positional): the local source dir
  first, then the `<WORKER>:<PATH>` agent endpoint. Exactly one side must
  reference an agent or remote host.
- Path resolution: mngr treats an argument as a *local path* only when it
  starts with `/`, `./`, `../`, or `~/` -- a bare `data/foo` is read as an
  *agent name* (hence the `./` on the source above). On an agent endpoint, a
  relative `<WORKER>:PATH` resolves against the worker's workdir; an absolute
  `<WORKER>:/PATH` is used verbatim.
- Use the directory form (trailing slash on both sides). mngr passes the paths
  through to rsync verbatim, so the trailing slash is load-bearing: it makes
  rsync copy directory *contents* into the destination instead of nesting the
  dir under it. Syncing a single file fails -- rsync wants a directory.
- `--uncommitted-changes=clobber` is required. Both endpoints routinely carry
  uncommitted local state, so the default `fail` mode would refuse the sync.
  `clobber` is safe here because every destination sits under gitignored
  `data/`: nothing tracked is overwritten, and nothing is pushed onto the git
  stash that every worktree of the repo shares.
- There is no `mngr file put` subcommand -- `mngr rsync` is the correct
  mechanism.
