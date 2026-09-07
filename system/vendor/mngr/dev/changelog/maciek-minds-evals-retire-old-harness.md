`apps/mngr_minds_eval`, the pre-harbor Minds persona eval harness, is deleted. `apps/minds_evals` supersedes it and is now the only one; its history is reachable with `git log --all -- apps/mngr_minds_eval`.

Consequences outside that directory:

- The workspace member is gone from the root `uv.lock`, and `--cov=imbue.mngr_minds_eval` is dropped from the root pytest addopts.

- `just minds-evals-generate` invokes the generator under its new console-script name, `minds-evals`.

- The workspace venv no longer provides a `minds-evals` command: the old launcher owned that console script as a workspace member, while the harbor generator that now carries the name lives in the standalone `apps/minds_evals` project, so it is reached as `uv run --project apps/minds_evals minds-evals` (or via `just minds-evals-generate`). Either way the whole old subcommand surface is gone: `minds-evals launch` / `stop` / `list-batches` / `inspect` / `evaluate` / `visit-batch` / `box`. `harbor view` and `harbor trial regrade` cover listing, reading and re-grading results, and a harbor run is stopped by stopping its runner. There is no replacement for `box`, which booted a desktop Minds computer on a branch tip (tracked on #708).

- `scripts/modal_nuke.py` stays. It is not the harness's: it nukes the apps and volumes of an mngr installation's own Modal environment, for when `<host_id>.json` state on the Modal volume has drifted and `mngr destroy` no longer works. The old harness was one caller among others, and it remains the tool for cleaning up leaked environments.

- `uncertainties.md` drops its entry about the old harness's `s3_store.py` layout comment, and `specs/minds-eval-harbor/concise.md` is updated to describe the conversion as complete.
