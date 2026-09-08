`just minds-evals-run` no longer pushes results to R2. Its `push_r2` parameter and the `MINDS_EVALS_PUSH_R2` environment variable are gone, along with the `aws s3 sync` step and its dependency on a `~/.minds-eval/r2.env` credentials file. Job results stay in `apps/minds_evals/jobs/<job>/`; archiving them is the job of whatever runs the eval on a schedule, with its own credentials.

Extra harbor args are now the recipe's **fourth** parameter rather than the fifth: `just minds-evals-run <dataset> <job> <concurrency> --ak snapshot_mode=per-turn`.
