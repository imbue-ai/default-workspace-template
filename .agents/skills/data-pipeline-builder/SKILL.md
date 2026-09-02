---
name: data-pipeline-builder
description: Build a simple, fast ingestion/processing tool that turns batches of raw records (JSON/CSV/Parquet dumps, periodic exports, API pulls) into a processed, queryable store, with incremental loading and backfill. Use when asked to ingest, load, index, ETL or process exported data, especially when batches arrive periodically and overlap.
metadata:
  author: imbue
---

# Building a data ingestion & processing tool

You are building a tool that turns batches of raw records into a processed store, repeatedly:
new batches keep landing (incremental) and history gets loaded newest-first (backfill).
Priorities, in order: correct, simple, fast, ergonomic. Follow the steps in order.

**Budget.** This procedure should cost about 10% more than writing the tool directly, not
double. The minutes in the step headings cap your own time on that step (they are not script
runtimes); when one is spent, ship what you have and say what you skipped. Build in three
writes after step 1: (1) the parse tests and `sources.py`; (2) `store`, `ingest`, `export`
and `cli` in one message; (3) the tests of step 8 and the README. No benchmarks, timings,
sweeps or parameter searches: the tool must be fast by construction (step 5), and nobody is
asked to measure it.

**NEVER run a full-data inspection or benchmark.** The step-1 script, every check and every
test run on a sample: the smallest batch, at most one more adjacent to it, or 100 records; never a
batch you picked because it was large. The only whole-dataset run in this procedure
is the single full load of step 8, started in the background. No oddity scans, tie checks, type
censuses, phase timings or "let me double-check across everything" passes, at any scale, beyond
the step-1 script; if a question needs the whole dataset, answer it from the tool's `status`
output and ingestion log after that load.

## 1. Profile the data (~3 minutes: read, one small script, run once)

Profile only what the consumer will do with the data. First read: the directory tree
(`find <root> -maxdepth 3 | head`), the file counts per batch (pick the smallest batch now; every
per-batch command below runs on it), and two or three raw records per source from the smallest
batch and one adjacent to it. From that, decide the *roles* of the fields that matter:

- **identity** candidates (a generated uuid usually; ticket keys, `(channel, ts)`, filenames
  and directory names usually are NOT unique), and the **version** field per source;
- fields the consumer **groups by** (project, status, priority, team, channel, assignee);
- fields it **plots or sorts by** (dates, timestamps) and **sums** (estimates);
- fields it **passes through** (ids, titles) and any **cross-reference** it extracts.

Then write one script of at most 60 lines that reads records from the smallest batch and the
one adjacent to it (nothing else, ever) and prints, per source and only for the fields above:

- identity: for each candidate, distinct values vs records, and how many values repeat
  *within* one batch (then it is not an identity; a repeat only across the two batches is a
  re-export); for the chosen identity, how many recur across the two batches and how often
  their version field ties;
- group-by fields: distinct values, null rate, and how many distinct values collapse when
  case-folded and stripped of punctuation (spelling variants fragment every chart);
- date and timestamp fields: null rate, unparseable count, min and max, and how many parse
  but fall outside a plausible range (a 12-digit "epoch seconds" stretches a timeline by
  centuries);
- numbers and pass-through fields: the value types seen (an int where a string is declared);
- cross-references: how many extracted values resolve to a known identity of the target;
- per batch: record count, unreadable files, and the version-date range.

Run it once. Its output, verbatim, is the "Data notes" section of the README. Do not extend
the script afterwards; if a number looks impossible, re-run it on a different small batch.

These are findings for the consumer, not fixes for the tool: store and export raw values;
grouping normalization, axis clamping and null bucketing belong to the rendering layer, and
the Data notes are how the viewer team learns they are needed. Skip what the consumer never
touches: the long-tail field inventory, body-text statistics, whether directory names agree
with records (never derive data from paths, then it cannot matter). Do not write that tied
copies differ unless your script says their versions differ.

## 2. Test-first parsing (~3 minutes)

Before writing any tool code, write `tests/test_parse.py` against a not-yet-implemented pure
function per source, `parse(record) -> row` (no I/O, no store). Make it **table-driven**: one
list of `(name, record, expected_row_fields)` tuples and a single parametrized test, so a case
is one line to add or edit. At most eight rows, one per oddity class step 1 showed (a list
where a string is declared and a bare string where a list is; an int where a string is; an
unparseable date; a timestamp that parses but is absurd; a missing optional field; a value
whose type the contract does not cover, which is nulled and logged, not raised). Then write
`parse` to make them pass, as the first module of the tool.

Coercion rules: a field the contract marks "as-is", or gives no coercion for, is emitted
unchanged, never coerced or nulled. Any rule that turns a non-null source value into null is
a judgment call: one row in the README's judgment-call table, with the count the ingestion
log reports for it. There is no second implementation: the projection is verified by these
tests and the step-8 spot check, the merge and ledger by the step-4 fixture, and the
assembled pipeline by the step-8 scenarios.

## 3. Choose storage by access pattern (~1 minute)

Three questions: how is it written (keyed upsert of a few fields per record / append-only /
rewrite-all), how is it read (point lookups and small aggregates from an app / scan-heavy
analytics), and what must survive an interruption. If the task fixes the answers, apply the
matching line and move on.

- keyed upserts + in-process reads + resumability -> **embedded SQLite** (stdlib,
  transactional, zero infrastructure). Store the **projection** (the fields consumers need),
  not raw records: 10-50x smaller; export and status become trivial scans.
- append-only, scan-heavy analytics over millions of rows -> Parquet (polars/pyarrow) or DuckDB.
- thousands of records read by one script -> a JSON/JSONL file; no database.
- never for a single-writer local tool: a server database, an ORM, a framework, a queue.

The raw inputs are the archive; the store must be rebuildable from them.

## 4. One merge rule makes incremental, backfill, idempotency and order-independence fall out

- **identity** = the verified unique key from step 1. A field the profile marked NOT an
  identity is never a dictionary key anywhere in the tool; any lookup by it maps to a *set* of
  records, and every derived count that joins through it is computed over that set.
- **version** = a totally ordered tuple `(record's own version field, batch id)`; unparseable
  -> lowest; a third component (source path) only if ties can otherwise remain ambiguous.
- **merge** = keep the max:
  `INSERT ... ON CONFLICT(id) DO UPDATE SET ... WHERE (excluded.version, excluded.batch) > (stored.version, stored.batch)`.
- **batch ledger**: fully-applied batches, written in the *same transaction* as the batch's
  rows. One batch = one transaction.

Newer batches are incremental, older batches are backfill and cannot regress newer versions,
reloading is a ledger lookup (skip by default; `--force` re-reads), any order gives the same
store. The supplied data usually cannot prove this (small samples have no overlap; in real
exports the newest version sits in the newest batch, so a wrong rule still passes), so build a
tiny synthetic three-batch fixture: tied copies that differ, an older batch carrying the newer
version, a batch loaded twice, at least one int-typed field, one unreadable file, one
non-object document, and one natural key shared by records in two different groups with one
cross-reference to it. Assert which version won per identity, that the int came back as an
int, that the two bad files were reported and left out, that both groups count the shared
key's cross-reference, and that the export is identical for every batch ordering; do not
hand-write full expected export rows. Ties are common in re-exports; a
wrong tie-break corrupts attribution while every count still looks right.

## 5. Parallelism: choose by data shape; keep the write path single (~1 minute)

Decide from the data shape (step 1's file counts and sizes); measure only if it fits none of
these. With many small files, reading dominates: per-file latency.

- under ~2k records: **no pool**; keep an inline path behind a threshold constant. If the
  deployment is small, the pool is what you delete, and the rest of this section is moot.
- many files or CPU-heavy parsing: `ProcessPoolExecutor(min(8, cpu_count))`; processes
  parallelize both the per-file latency and the CPU; threads do not help. Workers read + parse
  + project and return **only the projected rows**, in chunks of a few hundred files; bound
  the chunks in flight (2-3x workers); one pool reused across batches, created before any
  database connection, shut down in `finally`. Do not A/B the parser or the chunk size.
- remote sources (network FS, object storage, HTTP): threads or async.
- the parent is the **single writer**: one transaction per batch, WAL + `synchronous=NORMAL`,
  `page_size` before WAL, secondary indexes after bulk loads.

The serial write caps the speedup; accept it. No sweeps, searches, floors, A/B or timing runs.

## 6. Keep it small

One package; flat modules (`sources`, `store`, `ingest`, `export`, `cli`); about 600 and never
more than ~800 non-blank non-comment package lines (tests, `verify/` and documentation are not
budgeted; keep `verify/` under ~150 lines); stdlib only; the per-source
abstraction is one record `(name, columns, parse, version_key)`; no base class with one
subclass; no config layers; no materialized roll-ups unless the task requires them. Tunables
(workers, chunk size, inline threshold) are constants at the top of one module, overridable
from the CLI. No optimization without a stated reason in a one-line comment.

## 7. Ergonomics the callers will actually use

- `load <store> <path>...` accepts batch dirs *or* a dataset root and expands, sorts, dedups;
  skips ledgered batches; `--newest-first`; `--force`; creates the store if missing.
- nightly incremental = point `load` at the root again. Backfill = `load <store> <root>
  --newest-first`, resumable after a kill because of the per-batch transaction. Both are one
  line each in the README's usage.
- `status` prints the ledger, counts, date ranges and the ingestion-problem totals: the resume
  point and the freshness banner. Human-readable by default, `--json` for the same fields as
  one object, so the consumer's app and scripts read it without parsing text.
- consumers read the store in-process: document tables/columns and two query examples.
- messy values become null only where the contract allows it; never crash, never silently
  "repair", never normalize a group-by value in the store (that is the rendering layer's
  call). Where the contract itself produces something odd, implement it as specified and flag
  it with a count in the README. List each judgment call with the number of records it affects.

## 7b. Ingestion errors: drop, count, log; never abort, never hide (~50 lines)

A bad record must never stop a load, and must never vanish silently. Route every problem
through one sink, `problem(batch, where, field, reason)`, and:

- an unreadable file, non-object document, or record with no usable identity is **dropped**;
  a field that fails its coercion is **nulled**; a value whose type the contract does not
  cover (a dict where a list is declared) is nulled, not passed through. Each is one problem
  line: `{"batch", "path" or identity, "field", "reason", "sample": first 80 chars}`.
- the problems go to an ingestion log next to the store (`<store>.ingest.jsonl`, appended,
  one JSON line each) and their counts into the batch ledger row.
- `load` prints one summary line to stdout per batch: records read, loaded, dropped, fields
  nulled, and the log path. `status` (and `status --json`) reports the totals. `docs_ingested` counts loaded
  records only.
- exit non-zero only when nothing could be done (store unwritable, batch missing) or when a
  batch looks systematically broken (say more than 20% of its records dropped): print the
  log path and stop, so a schema change is not swallowed.

The point is one pass: run the load once, read the log, fix the parser for every reason it
lists, re-run with `--force`. One parse test asserts that a record failing a coercion is
logged and nulled rather than raised.

## 8. Verify (~4 minutes), then write it down

- first, start the one whole-dataset run in the background: a full load of the largest
  dataset into a scratch store, then `status`. Its loaded count must equal the input files
  minus the dropped ones in the ingestion log. Check it at the end; do not wait for it.
- `tests/test_pipeline.py`, **table-driven** like the parse tests: one list of scenarios, each
  a name and a sequence of `load` calls over the **small** dataset, and one parametrized test
  asserting the export equals the full-load export: reverse order; one incremental step then
  one backfill step; reload of a loaded batch. The step-4 fixture is a second table, one row
  per batch ordering. Adding a scenario is one line.
- **spot check**, one more test sharing no code with `parse`: for 20 random records of the
  small dataset, the exported row equals the raw record for every pass-through and simple
  scalar field (identity, ids, titles, statuses, dates as strings). Any difference is a bug or
  a row in the judgment-call table.
- README, at most 50 lines, written once: usage (full load, incremental, backfill, status,
  export, one line each), tables/columns and two queries, the step-1 output as Data notes, the
  judgment-call table (rule, contract line, count from the ingestion log), and the ingestion
  log format. No design rationale, no timings.
- say what you skipped because a step's time was up.

## Environment hygiene

- run throwaway scripts from the project directory, never a shared scratch directory.
- no orphaned workers: `shutdown()` in `finally`, never exit with futures pending; if you
  interrupt a run, kill only your own leftovers by pid; never `pkill` by pattern (other builds
  share the machine).
