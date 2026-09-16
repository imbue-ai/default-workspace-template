# The nightly minds_evals workflow runs more than one eval config

`.github/workflows/minds-evals-scheduled.yml` evaluates a list of **suites** instead of a single
eval config. A suite is one eval config plus the harness configs worth running it on, and the list
lives in `apps/minds_evals/configs/nightly_suites.json`; `minds-evals ci-matrix` composes the run's
cells across suites, pairs and arms, so the workflow still reads values and composes nothing.

- Every job, concurrency group, harbor job name, artifact name and summary file name carries the
  eval config's slug (its file name, e.g. `eval-config-time-to-mock`) beside the pair and the arm,
  so two suites cannot overwrite each other's names or summaries. A cell is
  `<pair>-<config slug>-<harness config>`; the oracle pass's artifacts end in `-oracle`.

- The oracle pass runs once per (pair, eval config) rather than once per pair, because the task it
  replays a canned transcript against and the criteria that grade it are the eval config's own. Each
  cell gates on the oracle of its own config.

- A suite's arm may ask for more than one `attempts`, which becomes harbor's `-k`. The flag is
  passed only above the default of one, and the count is deliberately not part of a green marker
  key.

- The `config` dispatch input now means "one ad-hoc suite instead of the checked-in list" and is
  empty by default (there is no `DEFAULT_CONFIG` env value any more; `NIGHTLY_SUITES` names the
  suite file). `harness_configs` overrides the arms of every suite the run evaluates.

- `notify` posts one Slack message per pair and eval config.
