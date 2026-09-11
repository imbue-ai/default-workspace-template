# Hardening a creation

The universal contract for the **background harden pass**: once the user has
signed off on a shape in the foreground, put in the thorough, expensive effort
to turn it into a hardened, committed, reviewed creation -- in the background,
off the interactive path. This contract is the part that is identical across
every operation (crystallize, update, heal) and every creation (a reusable
skill, an app, a service, the system interface).

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

Every run over a creation with a footprint -- an app with an `app.toml`, or a
skill -- computes that footprint and writes it to the path the task
frontmatter's `scope_file` names -- Step 1's `eval` exposes it as
`SCOPE_FILE`, and it sits beside your task file at
`data/.tasks/harden/<slug>/scope.json`. Compute it before any other work when
the creation already exists on disk; for a skill you are building from scratch
(a skill `crystallize`), compute it as soon as the skill directory exists,
since the command refuses a path that is not there:

```bash
mkdir -p "$(dirname "$SCOPE_FILE")"

# TYPE app (an app with an app.toml) -- from the manifest:
uv run app-manifest footprint system/apps/<package>/app.toml \
    --diff-base "$DIFF_BASE" --out "$SCOPE_FILE"

# TYPE skill -- by path:
uv run app-manifest footprint --for-path .agents/skills/<name> \
    --diff-base "$DIFF_BASE" --out "$SCOPE_FILE"
```

`DIFF_BASE` comes from the task frontmatter's `diff_base`: the commit the lead
recorded at dispatch as the one *before* the work being hardened began, so the
scope file's `diff` covers the committed change you are verifying, and -- once
you regenerate it -- your own commits too. Fail loudly if it is unset.

A creation with no manifest -- a pre-manifest app, a standalone service, the
system interface -- has nothing to resolve: its footprint is its own directory
plus its supervisord section, and the run carries no scope file.

The file records `primary` (the creation's own directories), `wiring` (the
`system/supervisord.conf` sections that run it), `references` (what its
manifest claims outside its directory -- a skill that drives it, a script, a
doc), `context` (paths to read but never change), `conventions`, `exclude` (a
hard denylist of globs), and `diff` (the branch's changed files, split into
those inside the footprint and `outside_footprint`). Two consumers read it:
the test selection in `type-app.md`, and the freshness check the lead runs
before merging (`.agents/shared/references/harden-contention.md`). Regenerate
it whenever the footprint
moves under you -- when you register a `[[references]]` entry, or when you add a
supervisord section -- and once more immediately before your final report, after
committing everything, so the `diff` it carries includes every commit you made
(the diff reads commits only; an uncommitted edit is invisible to it).

A non-empty `diff.outside_footprint` in that final scope file is a claim to
settle before you report. For each path, either add a `[[references]]` entry to
the app's `app.toml` -- when the file genuinely belongs to the creation -- or
name it in your final report under `Outside footprint:`, one line each on why it
changed on this branch. A skill run's one sanctioned edit inside an app
directory, the `[[references]]` entry it adds to that app's manifest, sits under
the scope file's `context` and is counted as inside the footprint.
## Splitting the pass across sub-workers

When the creation has genuinely independent areas -- a Flask app's backend and
its frontend, say -- you may split the pass across sibling sub-workers instead
of doing all of it yourself. Nothing prescribes the split: you decide it for the
creation at hand, or decide there is none worth making.

Launch each sibling with the launch-task skill exactly as a chat agent would
(`.agents/skills/launch-task/SKILL.md`). Name each sibling with your own worker
name as the prefix (`update-todo-backend`, `update-todo-frontend`): names are
unique across the host in every state, so a re-run of the same pass reaches
the same names only after the old pass has been superseded and destroyed. You
are its lead, and the rules that are yours are in
`.agents/shared/references/lead-proxy.md` under "When you are a worker
yourself": answer a sibling's `question` yourself or re-raise it to your own
lead, merge exactly one level with `--no-ff`, destroy a merged sibling and stop
a stuck one, and await it with `--timeout 60m`.

- Each sibling's task file carries the same `operation` and `type` as yours plus
  its boundary in prose; when your run carries a scope file, give the sibling its
  own `scope_file` path beside its task file and your `diff_base`, since Step 1
  fails loudly without them. Say in the body that it runs only its scope's tests and
  **skips the "Review gates" section below**, because you run that verification
  once on the merged result, and that a small out-of-scope edit is allowed but
  must be listed in its `done` report.
- When a sibling's `question` decides a shared interface, use your judgement per
  case; the default is to message the affected sibling with the decision
  immediately (`mngr message`) rather than let it find out at merge time.
- Merge the siblings in a fixed order and resolve any conflicts yourself. Then
  run exactly the verification a direct pass runs -- the "Review gates" section
  below, scoped to your own footprint rather than any sibling's -- once on the
  merged result, and report `done` with the same body a direct pass would.

Weigh the cost before splitting: every sibling pays a venv converge and a plugin
install before it does any work, and on a small creation that overhead can
exceed what the parallelism saves.

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

## Review gates

1. Ensure all in-flight changes have settled and are committed
2. Run the scoped test set below and fix what it flags with narrowly targeted
   changes

### The scoped test set

Three parts, in order, **each its own `pytest` invocation** -- passing two of
these path sets to one command makes `conftest` resolve to whichever it reaches
first and dies during collection. A bare `uv run pytest` from the repo root is
not one of them: it collects the whole monorepo -- on this workspace about 2,500
tests and several minutes -- to check a change that usually touches a handful of
files.

1. **The creation's own suite**, as your `type-<TYPE>.md` defines it.

2. **The repo guards** -- the cross-cutting checks no creation owns: manifest
   and registry consistency, template stacking, hook wiring, the meta-ratchets.
   Run them whatever you touched:

   ```bash
   uv run pytest system/*.py system/scripts
   ```

   A few hundred tests, well under a minute. This is what catches a change that
   breaks a contract the rest of the tree depends on, which part 1 by
   construction cannot see.

3. **The full suite, only when the change left the footprint.** Regenerate the
   scope file, then read `diff.outside_footprint`. Empty means parts 1 and 2
   cover the change. Non-empty means it reached code outside the creation, and
   whatever depends on that code is in neither set:

   ```bash
   jq -e '.diff.outside_footprint | length == 0' "$SCOPE_FILE" >/dev/null \
       || echo "changed files outside the footprint -- run the full suite"
   ```

A run that carries no scope file -- a pre-manifest app, a standalone service,
the system interface, per "The scope file" above -- cannot make that check, so
it runs parts 1 and 2 and then the full suite once.

If your own task file says your lead runs this verification on the merged
result -- the scoped-sibling case above -- run your own scope's tests and skip
the rest of this section.

When complete, report back to the lead.

## If you need to give up

If you cannot reach a tested, clean state (a dependency you cannot resolve, an
intended behavior you cannot pin down from the task file), emit a `stuck`
terminal report stating what blocked you and where the work stands. Do not
report `done` on a creation whose tests or gates do not pass. "Too
judgement-heavy" is never a valid reason to give up -- model judgement that is a
fixed part of the flow is scripted, not abandoned; only give up if the process
itself is unstable.
