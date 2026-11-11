# Hardening a creation

The universal contract for the **background harden pass**: once the user has
signed off on a shape in the foreground, put in the thorough, expensive effort
to turn it into a hardened, committed, reviewed creation -- in the background,
off the interactive path. This contract is the part that is identical across
every operation (crystallize, update, heal) and every creation (a reusable
skill, an app, a service).

You follow this file from the start of the pass to its last report: after a
compaction, re-read it, and your operation and type references, before you go
on.

## Contents

Every pass reads the sections marked "every pass". Each of the others opens
with the condition that sends you to it, and all but the last hand off to a
file of their own, which sits beside this one.

- The premise and the bar (every pass)
- Isolation (every pass)
- Reporting back to the lead (every pass)
- Parallelism & Sequencing (every pass)
- The scope file (every pass)
- Splitting the pass across workers of your own (the rule every pass applies;
  how to split is in `splitting-the-pass.md`)
- Testing and hardening contract (every pass)
- Concurrent work, pipelines and stored data (a creation that runs
  independent I/O-bound operations, works through many items, captures data,
  or keeps anything on disk; detail in `data-and-storage.md`)
- Review gates (every pass; a run that is not a clean pass continues in
  `test-gate-cases.md`)
- Carried work from a superseded pass (a task body with `## Carried work`;
  detail in `carried-work.md`)
- Catching up to a moved base (the lead asks you to catch up; detail in
  `catching-up.md`)
- A reverted milestone (the lead reverted a milestone it merged; detail in
  `catching-up.md`)
- If you need to give up (you cannot reach a tested, clean state)

## The premise and the bar

The user has already signed off on work in the foreground; the thorough pass has been **deliberately deferred**.
The task now is to prove the creation actually works under test, harden it, and pass the review
gates. The bar is that the creation is **genuinely well-tested and clean** -- not "it ran once."

When another skill the pass follows sets its own economy rules -- e.g.
`data-pipeline-builder`'s per-step minute caps -- this contract wins wherever they conflict:
an expired budget never justifies skipping tests, and never report `done` on a
creation whose tests were skipped because time ran out. Rules that do not
conflict with the bar (such as that skill's ban on benchmarks, timings, and
parameter sweeps) still hold during hardening.

## Isolation

Do all of this on an **isolated branch / worktree**. Nothing should touch the
live, user-facing state until the branch is merged. If the worktree has
no `.venv`, sync once before any `uv run`. If a fix needs a new dependency, add
it the normal way and commit the manifest changes so they appear in the merge.

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the reporting
procedure and the task-file frontmatter schema: you write the body to a file
and the launcher's `report` subcommand delivers it. Surface decisions the user
must make as `gate` reports and stop; end the pass with a terminal `done` or
`stuck` status. The operation reference names the exact gate and status values
its operation uses, and `question` is available on top of them on every pass.

## Parallelism & Sequencing

Write tests and optimize in parallel, if possible. Avoid running full test suites
unless necessary, such as at the very end. The long tail is often review passes
and full test suites at the end of hardening.

## The scope file

Every pass computes its footprint -- every creation in `CREATIONS` -- and
writes it to `SCOPE_FILE`, beside your task file at
`data/.tasks/harden/<slug>/scope.json`. Compute it before any other work when
the creations already exist on disk; when you are building one from scratch (a
skill `crystallize`), compute it as soon as its directory exists, since the
command refuses a creation that is not there:

```bash
LEAD_HEAD=$(git -C "${LEAD_WORK_DIR:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}" \
    rev-parse HEAD) || exit 1
uv run --no-sync app-manifest pass-scope $(printf -- '--creation %s ' $CREATIONS) \
    --diff-base "$DIFF_BASE" --lead-ref "$LEAD_HEAD" --out "$SCOPE_FILE"
```

`DIFF_BASE` comes from the task frontmatter's `diff_base`: the commit the lead
recorded at dispatch as the one *before* the work being hardened began, so the
scope file's `diff` covers the committed change you are verifying, and -- once
you regenerate it -- your own commits too. The lead commits on a branch other
chats commit to as well, so that diff also holds their work, landed between
`DIFF_BASE` and your dispatch. `--lead-ref` names the lead's checkout's `HEAD`
(`LEAD_WORK_DIR`; the repo's main worktree when it is unset), and from it the
command finds where your branch forked: the newest commit of the lead's
first-parent history your branch holds. The pass's own changes are what your
branch changes relative to that commit, every file of your own commits, and
every file of a commit the lead made in the window before it that touched a
creation in the pass: that is how
the change you are hardening reached your branch, and a commit of another
chat's that touched none of them is not yours. A milestone the lead merged
from you does not move the fork point, and a commit of the lead's you merged
in does, so the lead's work that came in with that merge is not yours unless
it touched a creation in the pass; your own commits from before it stay yours.

The file holds one entry per creation under `creations` -- its `primary` (its
own directories), `wiring` (the supervisord program blocks that run it, in the
files that declare them), `references` (what its manifest claims outside its
directory -- a skill that drives it, a script, a doc), `context` (paths to read
but never change), `conventions`, and `exclude` (a hard denylist of globs) --
and one `diff` for the whole pass: the changed files, split into those some
creation's footprint accounts for, `outside_footprint` (your own changes none
does), and `others` (the rest: other work in the window, which is not yours
to explain or test). `system/libs/app_manifest/README.md` describes both. The freshness check
the lead runs before merging reads the same footprints. Regenerate the file
whenever a footprint moves under you -- when you register a `[[references]]`
entry, or when you add a supervisord section -- and once more immediately
before your final report, after committing everything, so the `diff` it carries
includes every commit you made (the diff reads commits only; an uncommitted
edit is invisible to it).

A non-empty `diff.outside_footprint` in that final scope file is a claim to
settle before you report. For each path, either add a `[[references]]` entry to
the owning app's `app.toml` -- when the file genuinely belongs to it -- or name
it in your final report under `Outside footprint:`, one line each on why it
changed on this branch. A skill pass's one sanctioned edit inside an app
directory, the `[[references]]` entry it adds to that app's manifest, sits
under the skill's `context` and is counted as inside the footprint.

## Splitting the pass across workers of your own

Split the pass across workers of your own when the change has two or more
parts a user would name separately and each is a whole job once tested;
otherwise harden it yourself, since every worker pays a venv converge and a
plugin install before it does any work. A page with three views over an ingested
export splits: the interface is one worker's job and the ingestion is another's.
Two unrelated features split too. A single page whose backend only serves it
does not, nor do a few small files or a script. Split by job, not by creation:
a pass over two apps edited to work together is one job (the seam between them
is what most needs testing, and only a worker holding both sides can test it),
while a pass over two creations changed for unrelated reasons may be two.

Read `splitting-the-pass.md` once you have decided to split: it has how to
launch and name the workers, what each one's task file carries, and how you
merge and verify their work as their lead.

## Testing and hardening contract

- **Write or extend thorough tests** that assert on markers which are true if
  and only if the creation behaves correctly -- not just that it ran. Cover the
  real behavior, including empty and overflow states.
- **Add fixture-based tests for anything that parses external data** (HTML, JSON
  from third-party APIs, scraped pages, uploaded files). Live-data checks alone
  miss the class of bugs that only surface when a specific input shape hits the
  parser. Save 1-3 representative samples as fixtures and assert on the exact
  parsed shape.
- Keep behavior worth re-checking as committed tests; use ad-hoc manual checks
  only for purely visual things not worth a permanent test, and do not duplicate
  the same coverage in both.
- **Run every suite that applies** plus the relevant ratchets.

## Concurrent work, pipelines and stored data

Five rules, each with its own condition. They hold **whenever** the condition
is true: when you first build the creation, and equally when an `update` makes
it true or enlarges a store the creation already had. Read
`data-and-storage.md` before you harden a creation any of them applies to; it
has each rule in full under the same heading.

### Optimize independent work

Applies when the creation performs multiple independent I/O-bound operations:
data fetches, API calls, subprocess invocations. They run concurrently rather
than in a serial loop, with bounded concurrency and a deterministic output
order.

### Tolerate partial failure and support resumption

Applies to any multi-item pipeline. One failed item never sinks the whole run,
and results are written as they complete so a re-run resumes.

### Preserve and surface captured data

Applies when the creation captures data. Each record keeps its raw payload and
a reference to its source, durably, not only the processed fields (the
preserve-and-surface principle in AGENTS.md).

### Bound disk growth

Applies whenever the creation persists anything across runs. Every store it
writes has an explicit retention bound, enforced by the same run that writes
and covered by a test.

### Keep rebuildable data out of the backup

Applies whenever the creation keeps a store it can rebuild (downloads,
extracted archives, clones, caches). The store lives in its own directory,
with an empty `.nobackup` file the code creates along with the directory.

## Review gates

1. Ensure all in-flight changes have settled and are committed
2. Run the test gate below and fix what it flags with narrowly targeted
   changes

### The test gate

The gate is whatever the pass's change can reach, and a command prints it.
From the repo root, after committing everything, regenerate the scope file
with the command in "The scope file" above, then:

```bash
uv run --no-sync app-manifest select-tests --scope-file "$SCOPE_FILE"
```

The change it selects for is what the pass is responsible for: the changed
files some creation's footprint accounts for, and every one of the pass's own
changes, inside a footprint or not: what your branch changed and the files of
the change you are hardening ("The scope file" above). Other work that landed in the window (the
scope file's `others`) selects nothing, except that the lead's work you merged
in runs the suites of the pass's creations it reaches ("Catching up to a moved
base"). The selector reads commits, like the
scope file. It refuses to run while the working tree holds an uncommitted or
untracked change, naming it, and refuses a scope file whose diff does not run
to `HEAD`; regenerate the file and run it again.

It prints shell lines, each under a comment naming the changed paths behind
it. Run every line, in order, each as its own command: they include the
frontend build the browser tests need, and passing two pytest roots to one
invocation breaks collection. What it selects, and why, is in
`system/libs/app_manifest/README.md` ("Selecting tests"): the repo guards, the
changed packages' and skills' own suites with their ratchets, the suites of
whatever declares a dependency on them, the browser tests too of an app whose
Python changed, only the `browser` and `frontend` tests of the chat app or the
shell when its frontend alone changed (any other app runs whole), each changed
test file of those two on its own, and the frontend checks. A path of the
pass's outside every package and skill, or in a package the root project
depends on, brings in the full root suite; that is the gate working, not a
gap. Do not drop any line of it.

Read `test-gate-cases.md` before you act whenever the run is not a clean pass.
It covers, each under its own heading:

- **A failing test.** Rerun only the tests that failed before anything else;
  one that passes on that rerun without a change of yours is flaky, and goes on
  your report's `Flaky tests:` line.
- **A failing test you would change.** Run it on the tree before your change
  first, and report which of the two caused the failure.
- **A suite the selector left out.** When you have a concrete reason to think
  a suite it did not print can observe your change, run it and declare the
  coupling, or name it under `Undeclared couplings:` when the suite is
  built-in.
- **A regression the gate let through.** When your task fixes a break an
  earlier change made, check whether the selector would have picked the test
  that catches it.
- **A command that dies from a signal** (exit status 137 or 143, or `Killed`
  with no failure output). It is not a test failure until the shed ledger says
  it is not a shed; a shed is never a failure to fix in the code.

Memory pressure is never a reason to skip a command of the gate, or to report
`done` without it.

If your own task file says your lead runs this verification on the merged
result -- you are one of the workers a pass was split across -- run your own
scope's tests and skip the rest of this section.

When complete, report back to the lead.

## Carried work from a superseded pass

A task body with a `## Carried work` section replaces an earlier pass that
ended without being merged, and names the branches its lead kept that pass's
commits on. That work is part of your change: you merge each branch into yours
before you start hardening, harden it like the rest, and list it under
`Carried:` in your `done` report. Read `carried-work.md` before you start when
your task body has that section.

## Catching up to a moved base

After your `done`, the lead may find that its `HEAD` moved before it could
merge you, and reply asking you to catch up to a named commit. Your verified
work stands; extend it to the new base rather than starting over: merge the
commit into your branch, resolve every conflict there, harden what came in,
re-run the test gate over what the catch-up changed for the pass, and report
`done` again with a `Caught up:` section. Read `catching-up.md` when that reply
arrives, and follow its "Catching up to a moved base".

## A reverted milestone

When the lead reverts a milestone it merged, its reply names the revert commit
and says why. You bring the milestone back on your own branch, as a revert of
that revert, and fix its work there. Read `catching-up.md` when that reply
arrives, and follow its "A reverted milestone".

## If you need to give up

If you cannot reach a tested, clean state (a dependency you cannot resolve, an
intended behavior you cannot pin down from the task file), emit a `stuck`
terminal report stating what blocked you and where the work stands. Do not
report `done` on a creation whose tests or gates do not pass. "Too
judgement-heavy" is never a valid reason to give up -- model judgement that is a
fixed part of the flow is scripted, not abandoned; only give up if the process
itself is unstable.
