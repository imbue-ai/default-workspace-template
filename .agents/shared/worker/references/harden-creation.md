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

Another skill the flow follows may set time or cost limits, such as
`data-pipeline-builder`'s per-step minute caps. Where such a limit conflicts with this contract, this
contract wins: running out of time never justifies skipping tests, and a creation whose tests were
skipped for lack of time is never reported `done`. The other skill's rules that do not conflict
still apply during hardening, such as `data-pipeline-builder`'s ban on benchmarks, timing runs and
parameter sweeps.

## Isolation

Do all of this on an **isolated branch / worktree**. Nothing should touch the
live, user-facing state until the branch is merged. If the worktree has no `.venv`, sync once
before any `uv run`. If a fix needs a new dependency, add it the normal way and commit the manifest
changes so they appear in the merge.

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the reporting
procedure and the task-file frontmatter schema: you write the body to a file
and the launcher's `report` subcommand delivers it. Surface decisions the user
must make as `gate` reports and stop; end the run with a terminal `done` or
`stuck` status. The operation reference names the exact gate and status values
its flow uses, and `question` is available on top of them on every run.

## Parallelism & Sequencing

Write tests and optimize in parallel, if possible. Avoid running full test suites
unless necessary, such as at the very end.

## The scope file

Every run writes a scope file to `SCOPE_FILE` (`data/.tasks/harden/<slug>/scope.json`,
beside your task file). It holds the footprint of each creation in `CREATIONS`: the paths that creation
owns or touches, in the fields listed below. Write the scope file before any other work. When you are building
a creation from scratch (a skill `crystallize`), write it as soon as that creation's directory exists,
because the command refuses a creation that is not on disk:

```bash
LEAD_HEAD=$(git -C "${LEAD_WORK_DIR:-$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")}" \
    rev-parse HEAD) || exit 1
uv run --no-sync app-manifest pass-scope $(printf -- '--creation %s ' $CREATIONS) \
    --diff-base "$DIFF_BASE" --lead-ref "$LEAD_HEAD" --out "$SCOPE_FILE"
```

The command takes `DIFF_BASE` from your task frontmatter's `diff_base` (the commit
before the work being hardened began) and `--lead-ref` from the `HEAD` of the lead's checkout, as in the
command above.

The pass's own changes are yours to account for: you test them, and you explain in your report any
that no footprint covers. They are:

- everything your branch changed since it forked from the lead's branch, including every file of your
  own commits;
- every file of a lead commit in the commit window (between `DIFF_BASE` and where your branch forked)
  that touched a creation in the pass.

Any other change outside the pass's footprints is not yours to explain or test. The diff holds
such changes because other chats commit to the lead's branch too, and because a lead commit you merge in
brings the lead's other work with it.

The scope file holds two things:

- **one footprint per creation**, under `creations`, listing that creation's paths:
  - `primary`: its own directories;
  - `wiring`: the supervisord program blocks that run it, in the files that declare them;
  - `references`: what its manifest claims outside its directory, such as a skill that drives it, a
    script or a doc;
  - `context`: paths to read but never change;
  - `conventions`;
  - `exclude`: a hard denylist of globs, never to be touched;
- **one `diff` for the whole pass**, splitting the changed files three ways:
  - files some creation's footprint accounts for;
  - `outside_footprint`: your own changes that no footprint accounts for;
  - `others`: everything else, such as other chats' work in the commit window. It is not yours to
    explain or test.

`system/libs/app_manifest/README.md` describes every field.

The diff reads commits only, so an uncommitted edit is invisible to it. Regenerate the file:

- whenever a footprint moves under you: when you register a `[[references]]` entry, or add a
  supervisord section;
- once more right before your final report, after committing everything, so the diff includes every
  commit you made.

Before you report, check `diff.outside_footprint` in the final scope file. Every path
listed there needs one of these:

- if the file genuinely belongs to an app, add a `[[references]]` entry for it to that app's
  `app.toml`;
- otherwise, list it in your final report under `Outside footprint:`, with one line on why it changed
  on this branch.

One edit never needs this: the `[[references]]` entry a skill run adds to an app's manifest. It falls
under the skill's `context`, so it already counts as inside the footprint.

## Carried work from a superseded pass

A `## Carried work` section in your task body means an earlier pass on this work stopped
before the lead merged it. The section lists the branches that hold that pass's commits. That work is part
of your change. Before you start hardening, bring it into your branch in this order:

1. **If the section names a revert, revert that revert.** This happens when the earlier pass had a
   milestone (a commit the lead merged early) that the lead later undid with a revert commit. Reverting the
   revert puts the milestone's code back on your branch:

   ```bash
   git revert --no-edit <the revert commit>
   ```

   Do this before step 2. The milestone's commits are already in the lead's history, so merging the
   carried branch will not bring them back; it brings only the commits made after the milestone. Those
   later commits edit the milestone's code, so they apply cleanly only once that code is back.
2. **Merge each carried branch:**

   ```bash
   git merge --no-ff <the carried branch> -m "Carry over <the carried branch>"
   ```
3. **Resolve conflicts** as step 2 of "Catching up to a moved base" says, with one difference: here
   your side is the newer one, so fit the carried commits around what the user has done since.

Then harden the carried commits under this whole contract. For each one, keep it if it still holds,
fix it if it does not, or `git revert` it if the current base has made it wrong. Once merged they are
commits on your branch, so the scope file's diff and the test gate cover them with nothing extra.

Add a `Carried:` section to your `done` report: each carried commit, whether you kept, changed or
reverted it, and why.

## Splitting the pass across sub-workers

Split the pass across sibling sub-workers when the change has two or more parts a user would name
separately, and each part is a complete job once tested. Otherwise harden it yourself: every sibling sets
up a Python environment and installs plugins before it does any work. Some examples:

- split: a page with three views over an ingested export (the interface is one job, the ingestion
  another); two unrelated features; two creations changed for unrelated reasons;
- keep together: a single page whose backend only serves it; a few small files or a script; two apps
  edited to work together (the connection between them needs the most testing, and only a worker holding
  both sides can test it).

Launch each sibling with `.agents/skills/launch-task/SKILL.md`, as a chat agent would. Name each one with
your own worker name as a prefix (`update-roadmap-ingest`, `update-roadmap-frontend`). Names are unique
across the host, so a rerun of the same pass can reuse them only after the old pass is superseded and
destroyed. You are each sibling's lead; your rules are in `.agents/shared/references/lead-proxy.md`, "When
you are a worker yourself":

- answer a sibling's `question` yourself, or pass it up to your own lead;
- merge exactly one level, with `--no-ff`;
- destroy a merged sibling, and stop a stuck one;
- wait for a sibling with `--timeout 60m`.

Each sibling's task file carries:

- the same `operation` as yours, the `creations` its part touches, its own `scope_file` beside its task
  file, and your `diff_base` (Step 1 fails without them);
- its boundary, in prose;
- a note that it runs only its own scope's tests and skips "Review gates" below, because you run that
  once on the merged result, and that it may make a small out-of-scope edit if it lists the edit in its
  `done` report.

When a sibling's `question` decides an interface the siblings share, message the other affected sibling
with the decision right away (`mngr message`), so it does not find out at merge time. Use your judgement
for exceptions.

Merge the siblings in a fixed order and resolve conflicts yourself. Then run the verification a direct
pass runs ("Review gates" below, against your own scope file) once on the merged result, and report `done`
with the same body a direct pass would.

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

The foreground pass aims to get something working, so it often leaves independent operations running one
after another. Make the creation fast here. When it performs several I/O-bound operations that do not
depend on each other's results (data fetches, API calls, subprocess calls), run them concurrently. This
matters most for pipelines that fetch from many sources, where a serial loop is often most of the run
time.

Rules:

- Bound the concurrency with a semaphore or worker pool sized to what the source allows, and respect its
  rate limits. An unbounded fan-out that trips throttling or a ban ends up slower than the serial version.
- Parallelize only independent work. Keep steps serial where one needs another's output, where order
  matters, or where the source requires sequential access (cursor pagination, stateful sessions).
- Keep output deterministic: collect concurrent results and order them explicitly, so tests and pages stay
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
(see the preserve-and-surface principle in AGENTS.md). A pipeline that fetches,
transforms, and discards the raw payload cannot satisfy that principle no matter
what consumers do: persisting it is what lets a later change in processing
re-derive new fields with no refetch, and what lets surfaces show the raw record
or link out to its source. Retain whatever a consumer needs to render the record
faithfully later.

## Bound disk growth -- evict what the creation no longer needs

This applies whenever the creation keeps anything across runs: when you first build it, and when an
`update` makes a stateless skill or service start fetching or storing, or makes an existing store larger.
The sections above tell you to write records durably and incrementally. Without a limit, anything that
runs repeatedly (a recurring pipeline, a service that polls, anything that appends results, raw payloads,
logs, caches or fixtures) grows until it fills the disk. Give every growing store a retention limit; a
creation whose stores only grow does not pass hardening, however well tested.

- **List every store the creation writes, and set a limit for each.** Persisted results, raw-payload
  archives, on-disk caches, log files, generated fixtures, temp directories: for each, pick a maximum
  age, count, total size, or "keep only the latest N runs", and enforce it in the creation's own code.
- **Evict in the same run that writes.** Prune on write, or in a step every run reaches, so the limit
  holds without a separate or manual cleanup.
- **Evict raw records with their derived data.** Keep raw records long enough to re-derive and show
  them, up to the limit. When a record ages out, delete its derived data and its raw payload together, so
  no page links to a source that is gone.
- **Use existing rotation or TTL support first:** log rotation, a store's built-in TTL or size cap, or
  the repo's retention helpers, before your own prune loop. Never delete by a loose pattern (glob and
  `rm`) that could remove data the creation still needs.
- **Cover the limit with a test.** Write past the limit and assert the store holds only what the policy
  allows, with the oldest entries gone.

## Keep rebuildable data out of the backup

This also applies to every creation. The workspace's hourly backup (`host-backup`, a restic snapshot of
the whole home tree) takes time in proportion to how many files and directories it walks, whatever their
size. A store of a few hundred thousand small files adds minutes to every hourly backup, even when nothing
changed, and a size limit from the section above does not help: 8 GB of extracted source trees is a
million files. So mark every store the creation can rebuild (downloaded or extracted archives, clones,
fetched pages, thumbnails, model or package caches):

- **Give each rebuildable store its own directory** (for example `DATA_DIR / "repos"`), apart from
  records only the user could recreate.
- **Create an empty `.nobackup` file in that directory wherever the code creates it**, so the marker
  returns whenever the directory does. The backup then keeps only the marker:

  ```python
  def ensure_rebuildable_store_dir(path: Path) -> Path:
      path.mkdir(parents=True, exist_ok=True)
      (path / ".nobackup").touch()
      return path
  ```

  A `CACHEDIR.TAG` file has no effect here: the backup ignores it on purpose, because uv writes one into
  every virtualenv and tool environment.
- **Rebuild on demand.** Treat a missing or empty store as a cache to refill. After a restore the
  directory is gone or holds only the marker, because a restore deletes whatever the backup did not hold.
- **Mark only what can be rebuilt from its source.** Leave unmarked anything the user made, or anything
  that could not be fetched again (a source that may disappear, results of a paid call). When unsure,
  leave it in the backup.
- **Cover it with a test**: the directory holds the marker once the code creates it, and the code
  refills the store after it is deleted.

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

The selector picks tests for the pass's own changes and for every changed file a creation's
footprint accounts for (both defined in "The scope file" above). Other work in the commit window
(the commits between `DIFF_BASE` and the point where your branch forked from the lead's) selects
nothing; the scope file lists it under `others`. The one exception is the lead's work you merge in when
catching up, which runs the suites of the pass's creations it reaches ("Catching up to a moved base",
step 4). The selector reads commits only. It refuses to run while the working tree has an
uncommitted or untracked change, and names it. It also refuses a scope file whose diff stops short of
`HEAD`. In either case, commit, regenerate the scope file, and run it again.

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
uv run --no-sync app-manifest select-tests --diff-base <breaking commit>^ --diff-ref <breaking commit>
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
acts on,
`python3 system/services/oom_priority/bin/memory_candidates.py --free-memory`
(is it recovering since the shed, or still falling?). Not `/proc/meminfo` or
`free`: under a memory cap they read far more free memory than the guard
sees. If things are settling, rerun the shed command. If they are not, or
the rerun is shed too, ask your lead with a `question` gate
(`worker-reporting.md`) naming the command, the ledger records, and the free
memory you saw; your lead frees memory with the user, and answers when you can
rerun.

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
   uv run --no-sync app-manifest pass-scope $(printf -- '--creation %s ' $CREATIONS) \
       --diff-base "$PRE" --lead-ref "$LEAD_HEAD" --out "$CATCH_UP_SCOPE" || exit 1
   uv run --no-sync app-manifest select-tests --scope-file "$CATCH_UP_SCOPE"
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
