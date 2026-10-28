# Hardening a creation

The universal contract for the **background harden pass**: once the user has
signed off on a shape in the foreground, put in the thorough, expensive effort
to turn it into a hardened, committed, reviewed creation -- in the background,
off the interactive path. This contract is the part that is identical across
every operation (crystallize, update, heal) and every creation (a reusable
skill, an app, a service).

## The premise and the bar

The user has already signed off on work in the foreground; the thorough pass has been **deliberately deferred**.
The task now is to prove the creation actually works under test, harden it, and pass the review
gates. The bar is that the creation is **genuinely well-tested and clean** -- not "it ran once."

When another skill the flow follows sets its own economy rules -- e.g.
`data-pipeline-builder`'s per-step minute caps -- this contract wins wherever they conflict:
an expired budget never justifies skipping tests, and never report `done` on a
creation whose tests were skipped because time ran out. Rules that do not
conflict with the bar (such as that skill's ban on benchmarks, timings, and
parameter sweeps) still hold during hardening.

## Isolation

Do all of this on an **isolated branch / worktree**. Nothing should the
live, user-facing state until the branch is merged. If the worktree has
no `.venv`, sync once before any `uv run`. If a fix needs a new dependency, add
it the normal way and commit the manifest changes so they appear in the merge.

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the reporting
procedure and the task-file frontmatter schema: you write the body to a file
and the launcher's `report` subcommand delivers it. Surface decisions the user
must make as `gate` reports and stop; end the run with a terminal `done` or
`stuck` status. The operation reference names the exact gate and status values
its flow uses, and `question` is available on top of them on every run.

## Parallelism & Sequencing

Write tests and optimize in parallel, if possible. Avoid running full test suites 
unless necessary, such as at the very end. The long tail is often review passes
and full test suites at the end of hardening.

## The scope file

Every run computes the pass's footprint -- every creation in `CREATIONS` -- and
writes it to `SCOPE_FILE`, beside your task file at
`data/.tasks/harden/<slug>/scope.json`. Compute it before any other work when
the creations already exist on disk; when you are building one from scratch (a
skill `crystallize`), compute it as soon as its directory exists, since the
command refuses a creation that is not there:

```bash
LEAD_HEAD=$(git -C "${LEAD_WORK_DIR:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}" \
    rev-parse HEAD) || exit 1
uv run app-manifest pass-scope $(printf -- '--creation %s ' $CREATIONS) \
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
the lead runs before merging reads the same footprints
(`.agents/shared/references/harden-contention.md`). Regenerate the file
whenever a footprint moves under you -- when you register a `[[references]]`
entry, or when you add a supervisord section -- and once more immediately
before your final report, after committing everything, so the `diff` it carries
includes every commit you made (the diff reads commits only; an uncommitted
edit is invisible to it).

A non-empty `diff.outside_footprint` in that final scope file is a claim to
settle before you report. For each path, either add a `[[references]]` entry to
the owning app's `app.toml` -- when the file genuinely belongs to it -- or name
it in your final report under `Outside footprint:`, one line each on why it
changed on this branch. A skill run's one sanctioned edit inside an app
directory, the `[[references]]` entry it adds to that app's manifest, sits
under the skill's `context` and is counted as inside the footprint.

## Carried work from a superseded pass

A task body with a `## Carried work` section replaces an earlier pass that
ended without being merged, and names the branches its lead kept that pass's
commits on (`harden-contention.md`, "Superseding a pass whose worker is
gone"). That work is part of your change. When the section also names a
revert of a milestone the earlier pass had merged, revert that revert first: a
merge cannot bring back commits the lead's `HEAD` already holds, and a carried
branch's later edits to the milestone merge cleanly only onto the milestone
itself, so merging them before the revert would set them against its removal.
What returns is carried work like the rest:

```bash
git revert --no-edit <the revert commit>
```

Then merge each branch into yours before you start hardening:

```bash
git merge --no-ff <the carried branch> -m "Carry over <the carried branch>"
```

Resolve a conflict as a catch-up does (step 2 of "Catching up to a moved
base"), except that here your side is the newer one: the carried commits are
older work to fit around what the user has done since. Then harden them under
this whole contract like the rest of the change. Keep each one that still
holds, fix one that does not, and `git revert` one the current base has made
wrong, so every carried commit ends up merged either way. From then on they
are commits on your branch, so the scope file's diff and the test gate cover
them with nothing extra. Your `done` report gets a `Carried:` section: each
carried commit, and whether you kept, changed or reverted it, and why.

## Splitting the pass across sub-workers

Split the pass across sibling sub-workers when the change has two or more
parts a user would name separately and each is a whole job once tested;
otherwise harden it yourself, since every sibling pays a venv converge and a
plugin install before it does any work. A page with three views over an ingested
export splits: the interface is one worker's job and the ingestion is another's.
Two unrelated features split too. A single page whose backend only serves it
does not, nor do a few small files or a script. Split by job, not by creation:
a pass over two apps edited to work together is one job (the seam between them
is what most needs testing, and only a worker holding both sides can test it),
while a pass over two creations changed for unrelated reasons may be two.

Launch each sibling with the launch-task skill exactly as a chat agent would
(`.agents/skills/launch-task/SKILL.md`). Name each sibling with your own worker
name as the prefix (`update-roadmap-ingest`, `update-roadmap-frontend`): names
are unique across the host in every state, so a re-run of the same pass reaches
the same names only after the old pass has been superseded and destroyed. You
are its lead, and the rules that are yours are in
`.agents/shared/references/lead-proxy.md` under "When you are a worker
yourself": answer a sibling's `question` yourself or re-raise it to your own
lead, merge exactly one level with `--no-ff`, destroy a merged sibling and stop
a stuck one, and await it with `--timeout 60m`.

- Each sibling's task file carries the same `operation` as yours, the
  `creations` its boundary touches, its own `scope_file` beside its task file
  (Step 1 fails loudly without them), its boundary in prose, and your
  `diff_base`. Say in the body that it runs only its
  scope's tests and **skips the "Review gates" section below**, because you run
  that verification once on the merged result, and that a small out-of-scope
  edit is allowed but must be listed in its `done` report.
- When a sibling's `question` decides a shared interface, use your judgement per
  case; the default is to message the affected sibling with the decision
  immediately (`mngr message`) rather than let it find out at merge time.
- Merge the siblings in a fixed order and resolve any conflicts yourself. Then
  run exactly the verification a direct pass runs -- the "Review gates" section
  below, against your own scope file, not a sibling's -- once on the
  merged result, and report `done` with the same body a direct pass would.

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

## Optimize independent work -- parallelize what the incremental pass left serial

The foreground pass optimizes for getting *something* working, so it tends to
leave independent operations running one after another. The harden pass is where
you make the creation fast, since the expensive rethink is deliberately deferred
to here. **When the creation performs multiple independent I/O-bound operations
-- data fetches, API calls, subprocess invocations -- that do not depend on each
other's results, run them concurrently rather than in a serial loop.** This is
most impactful for fetch-heavy pipelines (the fetch-process-show shape), where a
serial loop over many sources is often the dominant cost and parallelizing it is
a large, cheap win.

Guardrails:

- **Bound the concurrency** and respect the provider's rate limits -- an
  unbounded fan-out that trips throttling or bans is slower and worse than the
  serial version. Use a semaphore / worker pool sized to what the source allows.
- **Only parallelize genuinely independent work.** Keep steps serial where one
  depends on another's output, where ordering matters, or where the source
  requires sequential access (cursor pagination, stateful sessions).
- Preserve deterministic output: collect concurrent results and order them
  explicitly rather than relying on completion order, so tests and surfaces stay
  stable.

## Tolerate partial failure and support resumption

A pipeline that touches many external sources will eventually hit a failure on
one of them -- a timeout, a rate-limit, a malformed record. The harden pass is
where you make the creation survive that gracefully instead of crashing. This
applies to any multi-item pipeline, but parallel fan-out makes it urgent, since
more operations in flight means more failure surface.

- **Isolate failures -- one failed operation must not sink the whole run.**
  Capture each item's outcome (result or error) independently, continue past a
  single failure, and report which items failed and why. A partial result over
  the sources that succeeded is far more useful than a crash that discards the
  ones that worked; surface the failures so the user knows the result is partial
  rather than silently dropping them.
- **Persist results incrementally so a run can resume.** Write each result to
  durable storage as it completes rather than accumulating everything in memory
  and saving at the end -- then a re-run (after a crash, a rate-limit pause, or a
  transient failure) picks up from what already landed instead of refetching
  from scratch. This dovetails with the preserve-and-surface contract below:
  persist the raw payload keyed by its source id, so the same store serves both
  resumption and later re-derivation.

## Preserve and surface captured data

If the creation captures data, persist each record's **raw payload and a
reference to its source, durably** -- not just the extracted/processed fields
(see the preserve-and-surface principle in CLAUDE.md). A pipeline that fetches,
transforms, and discards the raw payload cannot satisfy that principle no matter
what consumers do: persisting it is what lets a later change in processing
re-derive new fields with no refetch, and what lets surfaces show the raw record
or link out to its source. Retain whatever a consumer needs to render the record
faithfully later.

## Bound disk growth -- evict what the creation no longer needs

This is an always-applies invariant, not a creation-time step. It holds
**whenever** the creation persists anything across runs -- when you first build
it, and equally when an `update` teaches a previously stateless skill or service
to fetch or store, or enlarges a store it already had. The persist-and-preserve
contracts above tell you to write records durably and incrementally. Left
unqualified, that turns any creation you run more than once -- a recurring
pipeline, a service that polls on a schedule, anything that appends results, raw
payloads, logs, caches, or fixtures -- into an ever-growing store that
eventually fills the disk. The harden pass is where you give every growing store
a **bounded retention policy**; a creation that can only accrete and never
evicts is not hardened, no matter how well-tested its happy path is.

- **Find every store the creation writes to and decide, explicitly, what bounds
  each one.** Persisted results, raw-payload archives, on-disk caches, log
  files, generated fixtures, temp/scratch directories -- for each, pick a bound
  (a max age, a max count, a max total size, or "keep only the latest N runs")
  and enforce it as part of the creation's own flow, not as a chore you hope
  someone remembers.
- **Evict as part of the same run that writes.** Prune on write (or on a step
  the run always reaches) so the bound holds without a separate cleanup process.
  A retention policy that depends on out-of-band manual cleanup is not a bound.
- **Reconcile eviction with preserve-and-surface, don't skip it.** These pull in
  opposite directions on purpose: keep the raw source records long enough to
  re-derive and surface them, but cap how far back that retention goes. When a
  record ages out, evict the derived data and its raw payload together so a
  surface never points at a source that has been pruned out from under it.
- **Prefer existing rotation/TTL machinery over hand-rolled deletion.** Use log
  rotation, a store's built-in TTL/size cap, or the repo's existing retention
  helpers before writing your own prune loop -- and never delete by a fragile
  heuristic (glob-and-`rm`) that could take out data the creation still needs.
- **Cover the bound with a test.** Assert that after writing past the limit the
  store holds only what the policy allows and that the oldest entries are gone --
  the eviction path is exactly the kind of behavior that silently rots if
  nothing exercises it.

## Keep rebuildable data out of the backup

This is also an always-applies invariant. The workspace's hourly backup
(`host-backup`, a restic snapshot of the whole home tree) takes time in
proportion to how many files and directories it walks, not their size: a store
of a few hundred thousand small files adds minutes to every backup, every hour,
even when none of them changed. A byte cap from the section above does not help
here -- 8 GB of extracted source trees is a million files. So whenever the
creation keeps a store it can rebuild -- downloaded or extracted archives,
clones, fetched pages, thumbnails, model or package caches -- mark it:

- **Give the rebuildable store its own directory** (e.g. `DATA_DIR / "repos"`),
  apart from records only the user could recreate.
- **Create an empty `.nobackup` file in that directory wherever the code
  creates it**, so the marker comes back whenever the directory does. The
  backup then keeps only the marker:

  ```python
  def ensure_rebuildable_store_dir(path: Path) -> Path:
      path.mkdir(parents=True, exist_ok=True)
      (path / ".nobackup").touch()
      return path
  ```

  (A `CACHEDIR.TAG` does nothing here: the backup ignores it on purpose,
  because uv writes one into every virtualenv and tool environment.)
- **Rebuild on demand.** A missing or empty store is a cold cache to refill,
  never an error: a restore deletes whatever the backup did not hold, so after
  one the directory is gone or holds only the marker.
- **Mark only what really can be rebuilt from its source.** Anything the user
  made, or that could not be fetched again (a source that may disappear, past
  results of a paid call), stays unmarked. When in doubt, leave it in the
  backup.
- **Cover it with a test**: the directory holds the marker once the code has
  created it, and the code refills the store after it is deleted.

## Review gates

1. Ensure all in-flight changes have settled and are committed
2. Run the test gate below and fix what it flags with narrowly targeted
   changes

### The test gate

The gate is whatever the pass's change can reach, and a command prints it.
From the repo root, after committing everything, regenerate the scope file
with the command in "The scope file" above, then:

```bash
uv run app-manifest select-tests --scope-file "$SCOPE_FILE"
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

**A failing test.** Rerun only the tests that failed (their node ids, from the
same directory the line ran in) before anything else. Add `--no-cov` when the
line's run reported coverage (the chat app's, the shell's and Getting
Started's do): a run of only some tests fails the coverage floor. In the chat
app's or the shell's suite add `-m ''` too, or a browser test named in it is
deselected (AGENTS.md). A test that passes on that rerun without any change of
yours is flaky, not broken: name it on your report's `Flaky tests:` line
(your lead reports or tickets it) and move on, without rerunning its suite.
Check the shed ledger for its run first (below): a failure the ledger shows
was shed is a shed, handled as one, not a flaky test. Other than a shed
command's rerun, a suite runs whole again only after you commit a fix, and
then as whatever the fix reaches:
`select-tests --diff-base <the commit before the fix>`. Everything in that
range is your own commits, so it needs no scope file. Those lines are that
commit's whole gate; a suite they did not print is a widening, and goes
through the `question` gate like any other.

**A failing test you would change.** Before editing a test that failed, run
it on the tree before your change: with everything committed, `git checkout
--detach "$DIFF_BASE"`, rerun its node id the way the line ran it but with
`uv run --no-sync` (your branch's venv stays), then check your branch back
out. Say in your report whether your change caused the failure or it was
already there; a fix to a test that already failed is reported as a fix to a
pre-existing failure, not as part of your gate.

**A suite the selector left out.** The selector reads only declarations:
`pyproject.toml` and `package.json` dependencies, `uv.lock`, the programs in
supervisord blocks, and app manifests' `[[references]]`. A coupling none of
those shows is invisible to it. When you have a concrete reason to think a
suite it left out can observe your change (it runs your script as a
subprocess, calls your service over HTTP, reads a file your change writes),
run that suite too. When that suite is an app the workspace built and the path
lies outside the app, declare the coupling as part of your change: add the
path to the app's `app.toml` as a `[[references]]` entry, so the next change
to it selects the app without anyone having to notice. When the suite is
built-in, leave its declarations alone: a local edit to a built-in file only
makes divergence for the next update, and a reference would widen a built-in
app's footprint into other units' files. Such a coupling is a built-in defect
(AGENTS.md, "Updates"): name the suite and the path it observes in your `done`
report under `Undeclared couplings:`, and your lead includes it in its report
of built-in issues for the pass.

**A regression the gate let through.** When your task fixes something an
earlier change broke, and a test catches the break (one that already existed,
or the one you add), check whether the selector picks that test for the
earlier change:

```bash
uv run app-manifest select-tests --diff-base <breaking commit>^ --diff-ref <breaking commit>
```

If the test's suite is not among the lines, declare the coupling that would
have selected it, the same way.

**A command that dies from a signal.** Exit status 137 or 143, or `Killed`
with no failure output, is not a test failure until the shed ledger says it is
not a shed. Note the time before each command (`date -u
+%Y-%m-%dT%H:%M:%S`), and on a signal death look for sheds since then, with
the time you noted written into the lookup (a shell variable does not survive
from one command to the next):

```bash
STARTED_AT=2026-09-24T11:00:00  # the time you noted before the command
jq -c --arg since "$STARTED_AT" \
    'select(.type == "process_shed" and .timestamp >= $since)' \
    /home/user/workspace/data/.state/oom_priority/events/shed.jsonl
```

A record whose `pid` or `comm` is the command or one of its children (pytest,
an xdist worker, node, a browser) means the command was shed for memory
pressure: that is a shed, never a failure to fix in the code. A shed child can
also surface as ordinary test errors (a browser that vanished mid-test), so
run the same check when failures look unrelated to your change. Then judge
whether a rerun would be shed again, from the ledger (are shed records still
arriving, for processes other than yours?) and from the free memory the guard
acts on -- the first line of `python3
system/services/oom_priority/bin/memory_candidates.py`, not `/proc/meminfo` or
`free -m`. Is that figure recovering since the shed, or still falling? If
things are settling, rerun the shed command. If they are not, or the rerun is
shed too, ask your lead with a `question` gate (`worker-reporting.md`) naming
the command, the ledger records, and the free memory you saw; your lead frees
memory with the user, and answers when you can rerun.

Memory pressure is never a reason to skip a command of the gate, or to report
`done` without it.

If your own task file says your lead runs this verification on the merged
result -- the scoped-sibling case above -- run your own scope's tests and skip
the rest of this section.

When complete, report back to the lead.

## Catching up to a moved base

After your `done`, the lead may find that its `HEAD` moved before it could
merge you, and reply asking you to catch up to a named commit
(`.agents/shared/references/harden-contention.md`, "Catching up a stale
pass"). Your verified work stands; extend it to the new base rather than
starting over.

1. Commit anything outstanding and merge the lead's commit into your branch:

   ```bash
   git merge --no-ff <the commit the lead named> -m "Catch up to <its short sha>"
   ```

2. Resolve every conflict on your branch. Regenerate a lockfile from its
   merged manifest (`uv lock`, `npm install --package-lock-only`), never pick
   a side of it. For anything else, the commits you are bringing in are the
   user's latest word: keep what they do and fit your hardening around them.
   A conflict whose answer depends on intent you cannot see is a `question`
   gate. Conclude the merge with `git commit --no-edit`, which keeps the
   subject step 4 finds it by.
3. **Harden what came in.** Commits that touch a creation in the pass are now
   part of the change you are hardening, under this whole contract: tests
   that cover their behavior, the invariants above, and anything your
   operation reference requires of a change (a critical app's bundles are
   rebuilt and re-reported, `type-app.md`). Commits that touch none of them
   need nothing from you. When the reply names creations to add to the pass
   (another chat's change reached past what yours covers), add each to the
   `creations` line of your task file's frontmatter (`$TASK_FILE`), so every
   later parse includes it, load its type's reference if no creation of that
   type was in the pass, and harden it as part of the change.
4. **Re-run the test gate over what the catch-up changed for the pass**,
   after committing. Not over everything since `$PRE`, your branch's tip
   before the catch-up merge: in a shared workspace most of what came in is
   other chats' work on other creations, which step 3 leaves alone. Write a
   scope file for the catch-up alone, beside `$SCOPE_FILE` rather than over
   it, and run the gate from that:

   ```bash
   MERGE=$(git log --first-parent --merges -1 --format=%H --fixed-strings --grep "Catch up to")
   PRE=$(git rev-parse "$MERGE^1")
   LEAD_HEAD=$(git -C "${LEAD_WORK_DIR:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}" \
       rev-parse HEAD) || exit 1
   CATCH_UP_SCOPE="$(dirname "$SCOPE_FILE")/catch-up-scope.json"
   uv run app-manifest pass-scope $(printf -- '--creation %s ' $CREATIONS) \
       --diff-base "$PRE" --lead-ref "$LEAD_HEAD" --out "$CATCH_UP_SCOPE" || exit 1
   uv run app-manifest select-tests --scope-file "$CATCH_UP_SCOPE"
   ```

   The commit you merged is the fork point now, so what came in with it is
   the lead's work. It selects where a commit of it touched a creation in the
   pass (step 3's), where your branch changed the same file (every conflict
   is one), and, through the scope file's `incoming`, where it reaches a
   creation in the pass through something that creation depends on (a
   package, a `uv.lock` upgrade, a shared frontend package, or the root
   project's own `pyproject.toml` or `conftest.py` for a creation the root
   project collects): that runs the creation's own suite and nothing else,
   or for a shared script, which has none, the full root suite.
   What you committed after the merge is yours, selected like any change. A
   selection with nothing to run means no part of the pass moved. Do not
   re-run the gate over `$DIFF_BASE`: what it already passed still stands,
   unless step 3 added a creation, whose changes before `$PRE` this window
   does not hold; then run the gate from the regenerated `$SCOPE_FILE`
   instead, as in "The test gate". For a test that fails in this run, the
   tree before your change ("A failing test you would change") is the commit
   you merged, the catch-up merge's second parent, not `$DIFF_BASE`: a test
   that fails there was already failing at the lead's `HEAD`, and one absent
   there is your pass's own.
5. Regenerate `$SCOPE_FILE` and report `done` again, with a `Caught up:`
   section naming the commit you merged, each conflict and how you resolved
   it, any creation you added, and what the re-run gate ran. The lead's work
   that came in is in the scope file's `others`, not your
   `outside_footprint`.

## A reverted milestone

When the lead reverts a milestone it merged, its reply names the revert
commit and says why. Bring the milestone back on your branch as
`worker-reporting.md`'s "A milestone the lead reverted" says, resolve any
conflict as step 2 of "Catching up to a moved base" says, then fix the
milestone's work per the lead's reply.

The revert of the revert is yours, selected like any change, so your test
gate covers the milestone's work again. A pass still under way goes on as
before; one that had already reported `done` finishes with steps 3 to 5 of
that section, where the tree before your change is the lead's revert commit,
not the merge's second parent, which holds the milestone again. Either way
the work reaches the lead only with the next report of yours it merges.

## If you need to give up

If you cannot reach a tested, clean state (a dependency you cannot resolve, an
intended behavior you cannot pin down from the task file), emit a `stuck`
terminal report stating what blocked you and where the work stands. Do not
report `done` on a creation whose tests or gates do not pass. "Too
judgement-heavy" is never a valid reason to give up -- model judgement that is a
fixed part of the flow is scripted, not abandoned; only give up if the process
itself is unstable.
