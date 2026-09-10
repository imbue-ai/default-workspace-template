The evals run on a schedule. A nightly workflow generates a dataset against two mngr and workspace-template pairs (both repos' `main`, and the stable channel's released `minds-v<version>` tag on both), runs the oracle pass and one live pass of the small config on Modal, checks that every trial completed with no harness `error` statuses and its structural gates passed, uploads the job directories as an artifact, deletes exactly the Modal environments it created, and posts a summary to Slack. Judge scores are reported, never gated. Pairs that match the last green run are skipped. The README's "Scheduled CI" section documents it.

- `minds-evals generate` accepts a branch, a tag, or a full 40-hex SHA for `mngr_branch` and `dwt_branch`. Annotated tags are peeled to their commit, so the recorded SHA is always checkout-able; a name carried by both a tag and a branch resolves to the tag, and that choice is logged.

- New `--mngr-ref` and `--dwt-ref` options override the config file's `mngr_branch` / `dwt_branch` for one generation, so a scheduled run can pin a known-good mngr/template pair without editing the checked-in config. The task metadata records whichever ref was used.

- The eval driver takes `--ak user_id_prefix=<fragment>`, prepended to every trial's Modal user id and so to its environment name; a scheduled run passes `ci-<YYYYMMDDtHHMMSSz>-` so its own environments can be found afterwards. Invalid prefixes are rejected before any box boots.

- Every trial's Modal user id opens with `evals-`, so an eval's environment reads `minds-staging-evals-...` and is recognisable among the environments real staging workspaces live in. The whole id is budgeted to 48 characters so the environment name never reaches the length mngr would truncate. A full rename to `minds-evals-*` is blocked by the minds app's root-name validators; the README says why.

- A trial records the exact Modal environment it created as `modal_environment_name`, in `agent/state.json` and in its metadata, written early enough that a trial killed mid-run still says what to clean up. A trial still never deletes its own environment: that is where a failed trial is debugged from.

- New `minds-evals check-run <job_dir>`: decides whether a finished harbor job passed (every trial completed, structural gates held, nothing in the evidence bundle went unmeasured), exits non-zero when it did not, and writes a `--summary-md` step-summary table and a `--summary-json` machine-readable report. Judge scores are reported and never gated. Oracle runs are checkable the same way. A job harbor regraded from a hub trial id is checkable too: the cache harbor leaves under `.sources/` is not read as a trial. An evidence manifest whose entries cannot be read is reported rather than read as "nothing went unmeasured", and a judge criterion whose likert answer cannot be read is named rather than quietly left out of the report.

- A trial's Minds workspace is named `EVAL-<trial name>-<salt>`, so two attempts of one case are told apart even under a scheduled run's shared `user_id_prefix`.

- New `minds-evals ci-user-id-prefix --output <path>`: mints the `--ak user_id_prefix=` stamp a scheduled run passes, so the `ci-` marker the sweep scopes on and the timestamp it ages by are minted by the same module that parses them back.

- New `minds-evals cleanup-environments`: deletes either exactly the environments a job's own trials recorded (`--job-dir`), or, as a backstop, everything under a prefix older than a cutoff (`--sweep-prefix --older-than-hours`). A sweep prefix that does not contain `ci-` is refused, so the sweep cannot reach an environment a developer run created. Both honour `--dry-run`. Deletion goes through the Modal SDK and needs manage access to the workspace. A job-scoped pass reads each trial's `state.json` on its own and skips, with a warning, a trial it cannot read -- so a trial truncated by the crash being cleaned up after never takes the other trials' environments down with it.

- A dataset builds one Modal box image, keyed on the mngr SHA and the Dockerfile together and cached as a whole rather than per instruction: about two and a half minutes cold on Modal's builders, and seconds on a hit. Any change to the staged mngr clone rebuilds all of it, so reordering the Dockerfile's instructions saves nothing. The README records the per-step measurements.

- The box image's pnpm stage installs the node dependencies and the bundled binaries, and nothing else. It builds no CSS: `apps/minds` has no `build:css` script, so a step that called one could only ever fail.

- The `harbor-rewardkit` cooldown override in `pyproject.toml` is gone: rewardkit 0.2.0 is older than the two-week window, so the plain `exclude-newer` policy admits it on its own.
