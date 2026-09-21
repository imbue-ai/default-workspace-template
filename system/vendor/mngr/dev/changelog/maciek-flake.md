- A nightly workflow, `minds evals tests (repeated)`, runs the `apps/minds_evals` pytest suite five times undisturbed and once more with every core kept busy, and reports the passes as one result. A test that failed in some passes and passed in others shows up as flaky-recovered on a neutral `Minds Evals Tests (repeated)` check, and the daily flake sweep reads that check like any offload suite's. The workflow also runs on a PR that changes it, and can be dispatched with other pass counts.

- `just test-minds-evals-repeat [passes] [stressed_passes]` is the same run locally; it leaves the merged result in `test-results/junit.xml` and each pass's own junit under `test-results/minds-evals-passes/`.

- `scripts/junit_merge_passes.py` merges the junit files of repeated pytest sessions into one offload-shaped junit (one testcase per attempt, named by repo-relative node id), exiting 1 only when some test never passed.

- The toolchain, viewer build and Chromium install that the `minds_evals` test jobs need live in one composite action, `.github/actions/setup-minds-evals-tests`, which the PR job's path gate also watches.
