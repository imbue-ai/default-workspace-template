`apps/minds_evals/README.md` is a user guide and reference for the harness as it stands: setup, the
usage commands and run knobs, the eval-config schema, what outcome verification collects and what an
`error` entry means for a trial as against a `failed` one, how UI flows are executed and graded, the
scoring dimensions and reward composition, and token and cost accounting.

It also corrects what it says about the harness: `verification_timeout_seconds` defaults to 1800
seconds; `script` and `fresh_env: true` are rejected at generation time rather than accepted and
ignored; `harbor view` takes its folder positionally; a flow is scored on completion at trial time,
so its `expect` is the grade-time judge's ruling alone; extra harbor args are the run recipe's fifth
parameter, so `push_r2` must be supplied before them; and a trial's transcript, snapshots and
`usage.json` live under `<trial>/agent/`.
