Added `app-manifest run-tests`, which runs what `select-tests` selects for a change, in order, and reports how each command ended: passed, failed (with the tests still failing), flaky (its failed tests passed when rerun on their own, once), or shed (killed while the memory guard recorded a shed). Each command's output and a `summary.json` go under `data/.tasks/run-tests/`, and the run exits non-zero when anything failed or was shed.

Both commands take `--all`, which selects every suite: the frontend checks, the full root suite, and each own-root suite with all its markers.
