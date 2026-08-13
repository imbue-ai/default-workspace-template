# minds-evals

Harbor-based Minds persona evals. Each persona case from an eval config becomes one
[harbor](https://github.com/harbor-framework/harbor) task; a run drives real multi-turn
conversations against real Minds workspaces on Modal and grades the transcripts with a rewardkit
verifier. This app replaces the bespoke `apps/mngr_minds_eval` harness (which stays until the
comparison and removal PRs land).

## How a trial works

1. The task's environment is a **box**: a full Minds computer (the adapted box Dockerfile plus a
   staged shallow clone of mngr-internal at an exact SHA), built on Modal's builders and
   layer-cached per mngr SHA.
2. The **driver** (`MindsPersonaDriver`, a host-side harbor agent) starts the Minds backend inside
   the box with per-trial env: the Modal token pair parsed from your `~/.modal.toml`, a salted
   per-trial `MNGR__PROVIDERS__MODAL__USER_ID` scope, and `ANTHROPIC_API_KEY`.
3. The driver creates one **nested workspace** through the production path (Minds API ->
   `mngr create` -> Modal provider), then drives the case's turns: wait until the workspace chat
   agent is WAITING, send the turn (literal, or role-played by the decider model on
   `DECIDE_FROM_PERSONA`), wait for the reply, snapshot the workspace, and keep
   `/logs/agent/full_transcript.jsonl` + `state.json` current in the box.
4. The **verifier** (pure rewardkit, separate container) scores the transcript: three 1-10 likert
   judge criteria (conciseness, nontechnical_language, proactive), a binary wordiness guard, and
   structural gates (transcript parses, the agent engaged with distinct non-stub replies, all turns
   completed, not timed out) that zero the reward when they fail. The judge grades a **message-by-
   message** rendering (`judge_transcript.txt`: one `[USER]` block per client turn, one
   `[AGENT · message N]` block per agent message) that a grade-time pre-step rebuilds from
   `full_transcript.jsonl`, so conciseness is judged per individual message and `harbor trial regrade`
   re-scores captured trials under the current rendering. Timed-out trials score 0 with a
   `timed_out` marker in `reward-details.json`; a judge/grading-infrastructure failure errors the
   trial instead of recording a fake 0.

## Setup

- `~/.modal.toml` (run `modal token new` once) -- everything runs on Modal.
- `export ANTHROPIC_API_KEY=sk-ant-...` -- the decider (simulated user) and the judge.
- Always invoke harbor as `uv run harbor` from the monorepo root: harbor is a pinned dependency of
  this app, which both fixes the version and makes the driver import path resolvable. A bare
  `uvx harbor` runs in an isolated env that cannot import the monorepo packages.

## Usage

```bash
# 1. Generate a dataset (one harbor task per persona case) from an eval config
just minds-evals-generate apps/mngr_minds_eval/eval-config-small.json /tmp/minds-evals/datasets/small

# 2. Sanity-check the dataset end-to-end with the oracle (canned transcript; no Minds boot)
uv run harbor run -p /tmp/minds-evals/datasets/small -a oracle -e modal -y -o apps/minds_evals/jobs

# 3. Run the real eval (concurrency = simultaneous boxes; set it to the case count for one wave)
just minds-evals-run /tmp/minds-evals/datasets/small my-eval-run 9

# 4. Browse results
uv run harbor view -o apps/minds_evals/jobs

# Re-grade finished rollouts without re-running them (needs the task path)
uv run harbor trial regrade -p /tmp/minds-evals/datasets/small apps/minds_evals/jobs/<job>/<trial>
```

Each trial boots its own 6-CPU/16-GB box, so a full run is a **scheduled/nightly regression job,
not a per-PR gate**. Handy knobs:

- `-m/--model` selects the decider (simulated-user) model; default `claude-opus-4-8`.
- `--ak snapshot_mode=per-turn|final|off` controls workspace snapshot cadence (the run recipe
  defaults to `final`; pass `--ak snapshot_mode=per-turn` after the named args to override).
- `-k/--n-attempts N` runs each case N times (judge scores are statistical; use means).
- `just minds-evals-run <dataset> <job> <concurrency> true` (or `MINDS_EVALS_PUSH_R2=1`) syncs the
  job dir to R2 after the run; it defaults to off everywhere.

Results land in `apps/minds_evals/jobs/<job>/` (per-trial dirs with `result.json`, the transcript,
snapshots, and `verifier/reward-details.json`).

## Eval config

The old harness's schema, unchanged:

```json
{
  "mngr_branch": "main",
  "timeout_seconds": 3600,
  "avg_word_count_baseline": 120,
  "personas": [
    {"id": "todo-app", "persona": "...", "prompts": ["Build me ...", "Sounds good.", "DECIDE_FROM_PERSONA"]}
  ]
}
```

- `mngr_branch` is resolved to an exact SHA at generation time and recorded in each task's
  `[metadata]`; the box is built from that SHA.
- Each `prompts` entry is one turn: a literal string sent verbatim, or `DECIDE_FROM_PERSONA` (the
  decider role-plays the client from the persona plus the transcript so far; cannot be the first
  entry).
- `avg_word_count_baseline` feeds the verifier's wordiness guard (pass unless the average words per
  agent turn exceeds baseline * 1.1). A "turn" here is one client turn's merged agent reply; the
  driver records that per-turn average as `average_words_per_turn` in the trial metadata and, for
  observability only (no gate), the finer `average_words_per_message` (words per individual agent
  message, before the per-turn merge).

## Reward mapping

`reward = weighted mean(conciseness, nontechnical_language, proactive, wordiness guard)` -- likert
criteria normalized as `(raw - 1) / 9`, so raw judge scores stay recoverable (`raw = 9 * normalized
+ 1`; raw values are in `reward-details.json`) -- zeroed unless every structural gate passed. The
gate composition lives in `tests/test.sh` (`finalize.py`) because rewardkit's `reward.toml`
aggregations cannot express "binary gate zeroes a weighted mean"; all judging and scoring happens
inside rewardkit.

## Notes

- The box image build takes 10-20 minutes the first time on Modal's builders, then is layer-cached
  per mngr SHA. Keep per-case data out of `environment/` or the cache key diverges.
- Debugging: `uv run harbor task start-env -p <task> -e modal -i`, then `modal shell`. The old
  harness's live noVNC desktop URL does not exist here (harbor's Modal provider opens no tunnels),
  but the box still runs x11vnc/websockify for in-sandbox use.
- Cleanup: the driver destroys its nested workspace sandboxes in a `finally` block
  (`mngr list --ids | mngr destroy - --force`, scoped to the trial's own USER_ID); the nested
  sandboxes' `modal_eval` 3h timeout is the backstop if the runner dies hard.
- This app contains async code (`driver.py`, `minds_bridge.py`): harbor's agent and environment
  APIs are async, so the ratchets that normally forbid async/asyncio carry nonzero baselines here.
