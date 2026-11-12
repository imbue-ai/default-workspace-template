# Hardening a creation: concurrent work, pipelines and stored data

A conditional part of the harden contract (`harden-creation.md`, beside this
file). Read it when the creation performs multiple independent I/O-bound
operations, works through many items, captures data, or keeps anything on disk
across runs. That includes an `update` that makes one of those true of a
creation it was not true of, or enlarges a store it already had. Each section
opens with the condition its own rule applies under.

## Contents

- Optimize independent work -- parallelize what the incremental pass left serial
- Tolerate partial failure and support resumption
- Preserve and surface captured data
- Bound disk growth -- evict what the creation no longer needs
- Keep rebuildable data out of the backup

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
(see the preserve-and-surface principle in AGENTS.md). A pipeline that fetches,
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
