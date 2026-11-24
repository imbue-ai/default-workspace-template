# Hardening a creation

The universal contract for the **background harden pass**: once the user has
signed off on a shape in the foreground, put in the thorough, expensive effort
to turn it into a hardened, committed, reviewed creation -- in the background,
off the interactive path. This contract is the part that is identical across
every operation (crystallize, update, heal) and every creation (a reusable
skill, an app, a service).

A reference this file names without a directory sits beside it.

## Contents

- The premise and the bar
- Isolation
- Reporting back to the lead
- Parallelism & Sequencing
- The scope file
- Splitting the pass across workers of your own
- Testing and hardening contract
- Review gates
- If you need to give up

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
- **Read `data-and-storage.md` and harden concurrency, pipelines and stored
  data to it** whenever the creation performs multiple independent I/O-bound
  operations, works through many items, captures data, or keeps anything on
  disk across runs: when you first build it, and equally when an `update`
  makes one of those true or enlarges a store the creation already had.

## Review gates

The gates, and how to run them, are in
`.agents/shared/references/review-gates.md`: read it before you run them. This
section has what is a harden pass's own.

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
in runs the suites of the pass's creations it reaches (`catching-up.md`).
The selector refuses a scope file whose diff does not run to `HEAD`;
regenerate the file and run it again.

Run what it prints as `review-gates.md` says, and read its sections on a run
that is not a clean pass before you act on one. For a harden pass the tree
before your change is `$DIFF_BASE`, and a flaky test goes on your report's
`Flaky tests:` line.

If your own task file says your lead runs this verification on the merged
result -- you are one of the workers a pass was split across -- run your own
scope's tests and skip the rest of this section.

When complete, report back to the lead. Your `done` may not be the end of the
pass: when the lead's reply asks you to catch up to a commit, or names a revert
of a milestone it merged, read `catching-up.md` and follow it.

## If you need to give up

If you cannot reach a tested, clean state (a dependency you cannot resolve, an
intended behavior you cannot pin down from the task file), emit a `stuck`
terminal report stating what blocked you and where the work stands. Do not
report `done` on a creation whose tests or gates do not pass. "Too
judgement-heavy" is never a valid reason to give up -- model judgement that is a
fixed part of the flow is scripted, not abandoned; only give up if the process
itself is unstable.
