# Verifying against a copy of the data

An app's or service's persistent store, `data/.apps/<name>/` (whatever its
`DATA_DIR` resolves to), **is the user's real data**. The recurring, expensive
failure is not the code edit. It is *verifying* a change by writing test data
into the live store and then "cleaning up" with a delete or reset whose
predicate is too broad and takes real records with it. The delete is where the
data dies.

Read this when checking a change would write, change or delete data, when the
user must see a change before it lands, or when a change must rewrite the live
store. The sections run cheapest first.

## Contents

- Read-only verification needs no ceremony
- A throwaway instance over a copy of the data
- A preview from a worktree
- Never clean up by deleting from the live store
- Snapshot before a change that rewrites the live store
- An app without the overrides
- The copy does not isolate external effects

## Read-only verification needs no ceremony

Most changes (UI, copy, a backend read path) can be exercised with `curl` or
Playwright against the live app without writing anything, and a preview may
read the live store to *render*. The danger is only writes.

That settles the *data* question, not the timing one. SKILL.md's "verify
before the user can see it" still decides where a browser drive may run: a
read-only Playwright pass writes nothing, but with a window open it lands in
the user's view.

## A throwaway instance over a copy of the data

**If exercising the change must write, change or delete data, never point it at
the live store.** Boot a throwaway instance against a *copy* of the store on a
*spare* port, exercise it there, then tear it down with its copy. The shared
`.agents/shared/scripts/agents_shared/serve_isolated_instance.py` owns all of
it:

```bash
URL=$(uv run --no-sync .agents/shared/scripts/agents_shared/serve_isolated_instance.py up \
    --name <name>-test --cwd . \
    --port-env <PACKAGE_UPPER>_PORT \
    --copy data=data/.apps/<name> \
    --env '<PACKAGE_UPPER>_DATA_DIR={copy:data}' \
    --health-path /health \
    --project system/apps/<package> \
    -- <name>)
# ...exercise the change at "$URL" (curl / Playwright); it can write freely...
uv run --no-sync .agents/shared/scripts/agents_shared/serve_isolated_instance.py down --name <name>-test
```

- `up` copies the store into the instance's own space on disk (`--copy`),
  picks a free port, injects it through the `<PACKAGE_UPPER>_PORT` override
  together with the data-dir override pointed at the copy, waits for the
  instance to answer, and prints its URL. `down` stops it and deletes the copy.
- `--project` boots a user-built app (a standalone project with its own lock
  and venv) from its own venv, synced first, as its program does. For a
  built-in app, which runs from the root venv, drop it and run
  `-- .venv/bin/<name>`.
- **Never copy app data into `/tmp`.** In the workspace container `/tmp` is a
  small RAM disk (about 1 GiB): a copy of any real store fails part way with
  "No space left on device", and the partial copy fills `/tmp` for everything
  else until you delete it. `--copy` writes to disk, outside the backup, and
  refuses a copy that would not leave the disk room to spare. If it refuses,
  copy only what the test needs (`--copy` a subdirectory, or seed a small store
  for the test) or verify read-only.
- **To pick up a further edit, refresh in place. Do not tear down and `up`
  again.** A `down` and `up` cycle picks a new port, so the URL you hold would
  point at a dead one. `refresh` re-boots just the instance's own process on
  its existing port:

  ```bash
  uv run --no-sync .agents/shared/scripts/agents_shared/serve_isolated_instance.py refresh --name <name>-test
  ```

  A change that only alters files the running process reads from disk on each
  request needs no refresh at all. Reserve `down` for when you are finished.

The instance registers nothing with the workspace and has no window: it is
for your own checks, driven on its port.

This is the point of the `DATA_DIR` and `<PACKAGE_UPPER>_PORT` overrides: the
isolation you need is **data isolation, not code isolation**, and it is one
command, not a worktree. The live store is only ever *read* (once, to make the
copy), and the only delete lands on a disposable path where real data never
lived.

## A preview from a worktree

This is the second form of preview SKILL.md's step 1 names: the changed app
itself, shown to the user before the change lands. The worktree is the app's
code isolation. An app with an `app.toml`
needs nothing spelled out: its manifest's `[preview]` table (the scaffold
writes one; absent, the scaffold convention applies) says how a throwaway
instance boots, and one script boots it from a worktree, on free ports, over a
scratch copy of the directories the table names:

```bash
uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py up \
    --app <name> --worktree <dir>          # prints <name>-preview
uv run --no-sync workspace-layout open <name>-preview   # puts a window of it in front of the user
uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py refresh --app <name>   # after a rebuild, in place
uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py down --app <name>
```

- **Every `up` and `refresh` syncs the app's venv in the worktree and then
  starts the app from it**, so the preview serves the worktree's code: a
  built-in app from the worktree's root `.venv` (`uv sync --all-packages
  --frozen`), a user-built app from its own project's (`uv sync --locked`).
  That boot sync counts against the preview's health wait, so on a fresh
  `git worktree add` run the same sync there once before the first `up`.
- **The sync never relocks.** After changing a dependency in the worktree, run
  `uv lock` (in the app's directory, for a user-built app) before `refresh`.
  Otherwise a built-in app's preview boots without the change, and a
  user-built app's fails its `--locked` sync.
- If the app has a frontend, build it in the worktree too.
- `--with <sibling>` boots a sibling app's preview from the same worktree
  first and points the app's copied registry at it.
- `--instance-key <key>` names the instance the window opens on, for an app
  whose `open_path` takes one (a chat opens on a conversation).

For a contained change exercised against a data copy, the throwaway instance
above, run from the live tree, is the cheaper shape, and it is the only shape
for a service with no manifest.

## Never clean up by deleting from the live store

If you did leave a stray test record in the live store, leave it. An additive
junk record is a far cheaper mistake than a broad delete. Better: do not write
to the live store in the first place, and use the copy above.

## Snapshot before a change that rewrites the live store

If a change truly must rewrite the live store (a data migration you cannot run
on a copy), snapshot it to disk under `/var/tmp` first, run the change, confirm
the real data survived, and only then remove the snapshot:

```bash
du -sh data/.apps/<name>; uv run --no-sync disk-headroom /var/tmp    # the copy must leave several GB free
cp -a data/.apps/<name> /var/tmp/<name>-pre-<change>
# ...run the change, confirm the real data survived...
rm -rf /var/tmp/<name>-pre-<change>
```

If the copy would not leave the disk room to spare, free space first or ask the
user before changing the live store without one. Never snapshot into `/tmp`
instead.

The snapshot is a *recovery net*. Do **not** turn it into a routine "wipe live
and restore the backup" step: overwriting a running program's store tears its
state, and any real writes that landed during your test window are silently
lost on restore.

## An app without the overrides

An app or service that hardcodes `data/.apps/<name>/` and its listen port at
its call sites cannot be pointed at a copy. Add the two overrides the scaffold
gives an app, as part of your change, so the throwaway instance above works:

- the data-dir override,
  `DATA_DIR = Path(os.environ.get("<PACKAGE_UPPER>_DATA_DIR", "data/.apps/<name>"))`,
  with every read and write routed through it;
- the port override,
  `PORT = int(os.environ.get("<PACKAGE_UPPER>_PORT", "<assigned-port>"))`,
  with `PORT` bound in `run_simple`, never a hardcoded literal.

If you genuinely cannot, fall back to read-only verification plus the snapshot
net.

## The copy does not isolate external effects

Pointing at a data copy does not stop a test run from really posting to Slack,
calling a remote API, or sending a message. Guard those separately (a dry-run
flag, test credentials). The data copy protects only the local store.
