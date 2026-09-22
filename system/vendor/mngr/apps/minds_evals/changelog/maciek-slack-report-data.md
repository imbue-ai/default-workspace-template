`minds-evals check-run` reports every input a trial's reward was composed from, not the judges alone.

Each trial carries a `criterion_scores` list -- every criterion of every dimension of every step, programmatic checks beside the judges, each naming the step it was scored on and the kind that produced it (`programmatic`, `llm` or `agent`) -- and a `dimension_scores` list of what each dimension scored, the composed `reward` among them and one set per step on a stepped trial.

Every criterion value is rewardkit's normalized 0-1 score. A judge's own 1-10 likert answer stays in the trial's `reward-details.json` rather than in the summary, and is recoverable from the normalization (`raw = 9 * normalized + 1`).

Each trial also carries the three spans it timed itself over: `elapsed_seconds` (the whole trial, workspace creation included), `conversation_seconds` (first client message to last reply) and `reply_seconds` (the recorded turns' own reply times, summed). All three are cumulative across a stepped trial's steps, and each is `null` where the trial's records do not say.

The step-summary table carries them in three columns: `dimensions` (`gates 1.00, quality 0.80, reward 0.75`), `criteria`, grouped under the dimension that scored each (`gates: not_timed_out 1.00; quality: conciseness 0.78`), and `time` (`elapsed 612s / conversation 545s / replies 320s`); there is no separate judge column. An oracle trial, which runs no conversation, records no duration and its `time` cell reads `-`.

The Slack report's `judge scores` table holds the judges' criteria alone, each cell the normalized value; a stepped case's column heading carries the step it was scored on (`build/conciseness`), and the legend above the table states the scale.
