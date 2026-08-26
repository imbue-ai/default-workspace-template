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
   the box with per-trial env: the Modal token pair parsed from your `~/.modal.toml` and a salted
   per-trial `MNGR__PROVIDERS__MODAL__USER_ID` scope. No AI credentials go in that env.
3. The driver creates one **nested workspace** through the production path (Minds API ->
   `mngr create` -> Modal provider) and then **signs it in the way a user does**, by posting the
   credentials to the workspace's own `/api/claude-auth/submit-credentials` once
   `/api/claude-auth/status` answers. A workspace boots unauthenticated -- the product's create path
   supplies no AI credentials -- so this is the designed step, not a workaround, and it keeps the
   graded agent in the same shared config-dir regime real workspaces run in. The endpoint restarts
   the claude agents, so the driver waits for WAITING again before turn 1.
4. It then drives the case's turns: wait until the workspace chat agent is WAITING, send the turn
   (literal, or role-played by the decider model on `DECIDE_FROM_PERSONA`), wait for the reply,
   snapshot the workspace, and keep `/logs/agent/full_transcript.jsonl` + `state.json` current in
   the box.
5. The **verifier** (pure rewardkit, separate container) scores the transcript: three 1-10 likert
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
- `export ANTHROPIC_API_KEY=sk-ant-...` -- the decider (simulated user), the judge, and the
  credential the driver signs each workspace in with. Set `ANTHROPIC_BASE_URL` alongside it to sign
  workspaces in against a proxy instead of the Anthropic API directly.
- Always invoke harbor as `uv run --project apps/minds_evals harbor` (from the monorepo root; or
  plain `uv run harbor` from inside this directory). harbor is a pinned dependency of this app,
  which both fixes the version and makes the driver import path resolvable. A bare `uvx harbor`
  runs in an isolated env that cannot import this package.
- The `minds-evals-*` recipes below live in `private.just`, which the root `justfile` imports, so
  grepping `justfile` alone will not find them.

This app is a **standalone uv project**, not a member of the monorepo's uv workspace: it has its
own `pyproject.toml`, `uv.lock`, and `.venv`. harbor declares `rich>=14.1.0` and `modal>=1.5.1`,
and the workspace is held at `rich<14` (by `litellm[proxy]`) and `modal==1.4.3` (by
`imbue-mngr-modal`); uv allows one version per package per workspace, so a separate lock is the
only way harbor gets the dependencies it declares. Practical consequences:

- `uv sync --all-packages` at the repo root does not install this app; run `uv sync` from this
  directory (or `just test-minds-evals`, which does it for you).
- `just test-quick` / `just test-offload` skip this directory, and the root `ty check` skips its
  modules -- but not `imbue/minds_evals/resources/`, which runs in the box against the monorepo venv
  and is type-checked by the root workspace for that reason. Its tests
  and type check run under `just test-minds-evals`, which the `test-minds-evals` CI job invokes on
  any PR touching this app or the monorepo packages it depends on.
- `imbue/minds_evals/resources/` and `imbue/minds_evals/templates/` import packages this project
  deliberately does not depend on (`litellm` and `mngr_forward` in the box, `rewardkit` in the
  verifier container). They are shipped as source into environments that do have them, so this
  project's type check and coverage skip both directories.

## Usage

```bash
# 1. Generate a dataset (one harbor task per persona case) from an eval config
just minds-evals-generate apps/mngr_minds_eval/eval-config-small.json /tmp/minds-evals/datasets/small

# 2. Sanity-check the dataset end-to-end with the oracle (canned transcript; no Minds boot)
uv run --project apps/minds_evals harbor run -p /tmp/minds-evals/datasets/small -a oracle -e modal -y -o apps/minds_evals/jobs

# 3. Run the real eval (concurrency = simultaneous boxes; set it to the case count for one wave)
just minds-evals-run /tmp/minds-evals/datasets/small my-eval-run 9

# 4. Browse results
uv run --project apps/minds_evals harbor view -o apps/minds_evals/jobs

# Re-grade finished rollouts without re-running them (needs the task path)
uv run --project apps/minds_evals harbor trial regrade -p /tmp/minds-evals/datasets/small apps/minds_evals/jobs/<job>/<trial>
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

## Token and cost accounting

Every agent message in the workspace event stream carries its own `usage` block and `model`, so a
trial's cost is derived from the transcript the driver already collects -- nothing extra is read out
of the workspace, and a trial that timed out still accounts for what it spent.

The **workspace agent** (the thing under test) fills harbor's own fields: `n_input_tokens` (cache
inclusive), `n_cache_tokens` (tokens served from cache), `n_output_tokens` and `cost_usd` on the
trial's `agent_result`, and the matching `final_metrics` on the ATIF trajectory. The **decider** (the
simulated user) is the harness's own spend rather than a property of the agent, so it is reported
separately under `metadata.decider_usage` and never folded into those fields.

**Delegated work is not in the transcript.** The events endpoint serves main-session events only: a
subagent's turns go to a separate per-subagent stream, and work handed to a newly created worker
agent belongs to that agent's stream entirely. Neither reaches the transcript totals, so an agent
that delegates looks cheaper than one doing the same work inline. Trials that delegate are marked
`is_cost_complete: false`, with `delegated_call_count` (exact -- `Agent` tool calls) and
`worker_launch_count` (heuristic -- Bash commands that look like a worker launch) alongside. Treat a
flagged trial's transcript cost as a lower bound and do not compare it against an unflagged one.

**`--ak proxy=true` removes that gap.** The driver runs a LiteLLM proxy inside the box, reverse-
tunnels it so the workspace reaches it as a loopback address, and signs the workspace in against it
with a per-trial key rather than the upstream credential. Because the workspace's claude agents share
one credential, every call crosses that boundary -- including delegated ones -- so `usage_proxy.jsonl`
is the complete account and becomes the source for harbor's fields, with the transcript's figures
kept in `metadata.transcript_usage` for comparison. Measured on the small dataset: the two agree to
the cent on cases that delegate nothing, and on one that launched a worker the transcript saw 44
responses and $2.86 while the proxy saw 69 requests and $5.23. Both of those are standard-rate
figures from before fast-mode pricing landed, so the same trials would report about twice that today;
what the comparison is about -- the gap between the two sources -- is unaffected by the rate.

The driver keeps reading the event stream after the agent reports WAITING, and once more before the
transcript is written for the last time. The agent can still be working then -- the workspace's own
turn-end flow runs after the reply -- and those messages exist only in the workspace, so a driver
that stopped at the reply would lose them for good when the workspace is destroyed, leaving the
proxy metering requests the transcript has no messages for.

The proxy is also where the box's egress can be narrowed later: LLM traffic becomes one loopback
address, so nothing needs to reach `api.anthropic.com` from the workspace at all.

**Fast mode changes the price, not the token counts.** Minds runs its chat agent in fast mode by
default (it buys latency at a premium, which is worth it when a human is waiting), and fast mode
bills the same tokens at twice the standard rate -- $10/$50 per MTok against $5/$25 on Opus 5 and
Opus 4.8, with the cache multipliers stacking on top. It is chosen per request, so a model id alone
does not determine a price. The proxy records the tier per request and each tier's tokens are priced
at its own rate:

| `is_speed_observed` | `fast_message_count` | What the cost means |
|---|---|---|
| `true` | `0` | Exact: every request ran standard and is priced standard. |
| `true` | `> 0` | Exact: that many requests are priced at the fast-mode rate. |
| `false` | `0` | A floor. The tier was never observed, so everything is priced standard -- which is half the truth if the workspace was in fast mode, as by default it is. |

Only the proxy can see this (`--ak proxy=true`); the event stream carries token counts but no tier,
so a transcript-sourced total always reports the tier as unknown. The tier is read from the request
parameter litellm actually sent upstream rather than from `usage.speed` in the response: litellm
blanks that field when it normalizes the response, and the raw body survives only for non-streaming
calls, while workspace traffic streams. Note also that **fast mode is Opus-only** -- the API rejects
`speed` outright on Sonnet and Haiku -- so a per-model comparison that leaves it on is not comparing
like with like, and switching tier invalidates the prompt cache.

`agent/usage.json` carries the full breakdown: the four non-overlapping token buckets (uncached
input, output, cache read, cache write), per model, with costs. The buckets stay separate because
Anthropic prices them differently -- a cache write costs 1.25x a plain input token and a cache read
0.1x -- so a single "input tokens" number can neither produce a correct cost nor show cache
behaviour. Prices come from `mngr_usage`'s table, which a drift test binds to the LiteLLM proxy's,
and an unpriced model reports `cost_usd: null` rather than a misleading `0`. A LiteLLM model entry
carries a single price, which has two consequences: the drift test covers the standard rates only,
because the proxy has no fast-mode price to compare against, and the per-request `cost_usd` inside
`usage_proxy.jsonl` is LiteLLM's own figure and always standard-rate. The trial's totals are the
tier-aware ones.

The cache-write rate above is the one for a 5-minute cache; Anthropic bills a 1-hour write at 2x an
input token instead of 1.25x. Nothing in the chain carries the TTL, so every write here is priced as
if it were 5-minute, and a trial whose agent asks for the longer cache understates that bucket by
37.5%.

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
- `dwt_branch` (on `dwt_repo`, the workspace template; defaults to `main` on
  `imbue-ai/default-workspace-template`) is pinned the same way: generation resolves it to an exact
  SHA, records it as `dwt_sha` in `[metadata]` next to the branch it came from, and the box clones
  that SHA. So a dataset builds the same workspaces however long after generation it is run --
  **picking up new template changes requires regenerating the dataset**, and datasets generated
  before `dwt_sha` existed no longer run (regenerate them). Each trial's own record carries
  `mngr_sha` and `dwt_sha` too (in `state.json` and the agent metadata), so a captured trial says
  which mngr and which template produced it.
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
- Debugging: `uv run --project apps/minds_evals harbor task start-env -p <task> -e modal -i`, then
  `modal shell`. The old harness's live noVNC desktop URL does not exist here (harbor's Modal
  provider opens no tunnels), but the box still runs x11vnc/websockify for in-sandbox use.
- Cleanup: the driver destroys its nested workspace sandboxes in a `finally` block
  (`mngr list --ids | mngr destroy - --force`, scoped to the trial's own USER_ID); the nested
  sandboxes' `modal_eval` 3h timeout is the backstop if the runner dies hard.
- This app contains async code (`driver.py`, `minds_bridge.py`): harbor's agent and environment
  APIs are async, so the ratchets that normally forbid async/asyncio carry nonzero baselines here.
