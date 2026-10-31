- **Python floor:** the root project type-checks every skill's scripts except a third party's skill, and the skill projects require Python 3.12.

- **update-self on an older Python:** the staged `update_self.py` now checks the interpreter before importing anything. On a workspace whose system Python is older than 3.12 (a local workspace from before 2026-09-14), it exits with code 75 and prints the whole procedure: stop the pass, record a `NEEDS_RECREATION` verdict with the workspace's own copy, tell the user it is a migration-required update (`migrate-workspace` moves their work), and release the update lease. Step 3a says only to stop and follow what a failing re-check prints.

- **Migration reference:** a new section on fixing ty errors in a workspace's own skills and apps. The update-self worker fixes them during the update, except an error whose only fix changes behavior the user can see, which it raises with the user instead. The command rewrites also cover the user's scheduled jobs in `data/.state/cron.d/`, which no check scans.

- **Syncing after a merge:** after merging a worker's branch that changed a `pyproject.toml` or `uv.lock` (such as a newly crystallized skill's `python/` project), the lead now runs `uv sync --all-packages --frozen`, so the new package is installed before anything runs it with `uv run --no-sync`. `running-python.md` lists this case. The update-self worker does the same before its validation suites and boots whenever the merge changed a `pyproject.toml` or `uv.lock`, including one only upstream changed, since those suites run `uv run --no-sync`.

- **Harnesses without a session-start sync:** `running-python.md` now says agents on harnesses without a session-start hook (claude and codex have one) sync themselves after a `pyproject.toml` or `uv.lock` change.

- **Ledger pointers:** publish-template, update-installed-template and update-published-template point at `update-self`'s `python/update_self_skill/update_ledger.py` for the exact `VERSION_HISTORY.md` starter block, instead of the `scripts/update_self.py` entry stub.

- **New apps at the 3.12 floor:** build-app's scaffold writes `requires-python = ">=3.12"` into a new app's `pyproject.toml`.
