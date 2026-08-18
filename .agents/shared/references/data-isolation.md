# Isolating a creation's data

Any creation that persists anything -- an app, a background service, a skill that
keeps state -- writes under `data/`, and **that is the user's real data**. This
reference owns two things: keeping that data safe while you verify a change, and
keeping the verification itself honest. It applies wherever the work happens --
the live change loop in a chat (`update-app`), or a background worker on its own
branch (the harden pass).

## A branch isolates code, not data

`data/` is gitignored, so a fresh worktree starts with an **empty** one while the
user's real records stay in the live tree. The isolation you need here is data
isolation, and a worktree does not give it to you in either direction:

- It does not protect the live store. A check that resolves `data/.apps/<name>/`
  writes to the user's records from a worktree exactly as it would from the live
  tree.
- It makes a check that *reads* the live store vacuous. The code path that reads
  real records never runs, so the suite reports success without exercising the
  thing it exists to cover.

So before believing a green run, **check whether anything under test reads
`data/`**. Anything that does needs data given to it deliberately -- an isolated
directory it populates itself, a copy of the real store, or a fixture that
matches a real record's shape -- rather than reading whatever happens to be on
disk.

## Protecting the live store while you verify

The persistent store -- `data/.apps/<name>/`, whatever `DATA_DIR` resolves to --
is the user's real data. The recurring, expensive failure mode is not the code
edit: it is *verifying* a change by writing test data into the live store and
then "cleaning up" with a delete/reset whose predicate is too broad and takes
real records with it. The delete is where the data dies. Encode these, cheapest
first:

- **Read-only verification needs no ceremony.** Most changes (UI, copy, a
  backend read path) can be exercised by curl/Playwright against the live
  service without writing anything. Reading the live store -- including to
  *render* a preview -- is fine; the danger is only writes.

- **If exercising the change must write, mutate, or delete data, never
  point it at the live store.** Copy the store to a scratch path *outside*
  `data/` (so it is neither served by the live service nor backed up), boot a
  throwaway instance against the copy on a *spare* port, exercise it there,
  then delete the *copy*. The shared
  [`serve_isolated_instance.py`](../scripts/serve_isolated_instance.py)
  script owns the boot + teardown -- it picks a free port, injects it (via the
  `<PACKAGE_UPPER>_PORT` override) plus your data-dir override, waits for the
  instance to answer, and prints its URL:

  ```bash
  cp -r data/.apps/<name> /tmp/<name>-scratch
  URL=$(python3 .agents/shared/scripts/serve_isolated_instance.py up \
      --name <name>-test --cwd . \
      --port-env <PACKAGE_UPPER>_PORT \
      --env <PACKAGE_UPPER>_DATA_DIR=/tmp/<name>-scratch \
      --health-path /health \
      -- uv run <name>)
  # ...exercise the change at "$URL" (curl / Playwright); it can write freely...
  python3 .agents/shared/scripts/serve_isolated_instance.py down --name <name>-test
  rm -rf /tmp/<name>-scratch      # deleting a copy can't harm real data
  ```

  It is a copy-plus-one-command setup, not a worktree. The live store is only
  ever *read* (once, to make the copy); the only delete lands on a disposable
  path where real data never lived. (`update-app` documents how to surface that
  same throwaway instance to the user as a labeled preview tab, when a change is
  worth showing before it goes live.)

- **Never "clean up" test data by deleting from the live store.** If you
  did leave a stray test record in it, leave it -- an additive junk record
  is a far cheaper mistake than a broad delete. Better: don't write to the
  live store in the first place (use the copy above).

- **Snapshot before any genuinely in-place change to the real store.** If a
  change truly must rewrite the live store (a data migration you can't run
  on a copy), `cp -r data/.apps/<name> /tmp/<name>-pre-<change>` first, run the
  change, confirm the real data survived, and only then remove the snapshot.
  The snapshot is a *recovery net* -- do **not** turn it into a routine
  "wipe live and restore backup" step: overwriting a running service's store
  tears its state, and any real writes that landed during your test window
  are silently lost on restore.

- **The copy isolates local state, not external effects.** Pointing at a
  data copy does not stop a test run from really posting to Slack, calling a
  remote API, or sending a message. Guard those separately (a dry-run flag,
  test credentials) -- the data copy only protects the local store.

## Resolve the store's path injectably

Everything above depends on the creation being *pointable* somewhere else. Two
overrides, both of which the `build-app` scaffold emits:

```python
DATA_DIR = Path(os.environ.get("<PACKAGE_UPPER>_DATA_DIR", "data/.apps/<name>"))
PORT = int(os.environ.get("<PACKAGE_UPPER>_PORT", "<assigned-port>"))
```

Route every read and write through `DATA_DIR`, and bind `PORT` in `run_simple`
rather than a literal.

- **Tests get their own directory through that same override**, set for the
  process under test before the module reads it -- exactly what
  `serve_isolated_instance.py` does with `--env`. A test that reads whatever
  happens to be on disk is not deterministic, and worse, it *passes* against an
  empty store while never exercising the code path it exists to cover.
- **Fixtures must be accurate.** A fixture that does not match a real record's
  shape buys confidence the code has not earned.
- **Retrofit older creations when you touch them.** One that predates this
  convention hardcodes `data/.apps/<name>/` and its listen port at the call
  sites; add both overrides as part of your change, so the throwaway instance
  above works. If you genuinely can't, fall back to read-only verification plus
  the snapshot net.
