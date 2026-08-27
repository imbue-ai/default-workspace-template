Under `--ak proxy=true`, a trial's ATIF trajectory reports the same usage as its `agent_result`: the proxy's complete account, including delegated work, rather than the transcript's understated one. Every writer of a usage figure -- harbor's fields, `agent/usage.json`, and `trajectory.json` -- resolves the source once, so the three cannot disagree about one trial.

`minds-evals-harbor generate --config` now lists `verification_timeout_seconds` among the config keys it accepts, and several source comments that had drifted from the code they describe are corrected.
