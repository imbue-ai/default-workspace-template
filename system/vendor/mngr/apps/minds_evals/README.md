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
5. Once the last turn is done and while the workspace is still alive, the driver runs an
   **evidence-collection** phase: it records what was actually delivered (the app registry,
   supervisord's view of it, a file inventory, HTTP probes, declared test commands, and the
   delivered repo as a git bundle) into `/logs/agent/verification/`. This has to happen here --
   the verifier runs after the workspace is destroyed. See [Outcome verification](#outcome-verification).
6. The **verifier** (pure rewardkit, separate container) scores the transcript: three 1-10 likert
   judge criteria (conciseness, nontechnical_language, proactive), a binary wordiness guard, and
   structural gates (transcript parses, the agent engaged with distinct non-stub replies, all turns
   completed, not timed out) that zero the reward when they fail. The judge grades a **message-by-
   message** rendering (`judge_transcript.txt`: one `[USER]` block per client turn, one
   `[AGENT · message N]` block per agent message) that a grade-time pre-step rebuilds from
   `full_transcript.jsonl`, so conciseness is judged per individual message and `harbor trial regrade`
   re-scores captured trials under the current rendering. Cases that declare `expectations` gain a
   third `outcome` dimension over the collected evidence. Timed-out trials score 0 with a
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
- `--ak verifier_model=<model>` runs the UI-flow verification agent on a different model from the
  decider (default: the decider's). Flow driving is mechanical, so a cheaper tier may do -- measure
  flow stability before changing the default.
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
behaviour. Prices come from `mngr_usage`'s table, which `litellm_pricing_test` pins entry by entry
against litellm's own price map, and an unpriced model reports `cost_usd: null` rather than a
misleading `0`. `build_model_list` derives the in-box proxy's config from that same table, one
flat-priced entry per model, so the per-request `cost_usd` inside `usage_proxy.jsonl` is a
standard-rate figure computed from those four buckets; the trial's totals are the tier-aware ones,
because `compute_cost` applies `FAST_MODE_PRICE_MULTIPLIER` on top.

That multiplier is the seam worth knowing about. litellm's map carries the fast premium itself, as
`provider_specific_entry.fast` -- so it *is* pinnable, and `litellm_pricing_test` does not pin it:
that test compares the four flat buckets only. The map also carries dimensions nothing here mirrors,
notably `cache_creation_input_token_cost_above_1hr` (the 1-hour cache-write rate, against the
5-minute rate every figure here assumes) and a regional uplift. Measured against the map on
2026-08-20, `FAST_MODE_MODELS` and `FAST_MODE_PRICE_MULTIPLIER` already disagree with it: litellm
gives a fast entry to four Opus models where this table names two, and prices the fast tier on Opus
4.6 and 4.7 at 6x rather than 2x. Neither is a model the eval runs today, so no recorded figure is
affected -- but treat the fast-mode rate as unpinned rather than as verified.

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
- `verification_timeout_seconds` (default 600) is the evidence-collection phase's own budget. It is
  *added* to the task's `[agent].timeout_sec` (case timeout + verification budget + grace), so
  verification never competes with the conversation for time.
- Each persona entry may carry an `expectations` block; see below.

## Outcome verification

Without this, the eval grades only how the agent *talks*: an agent that chats beautifully and ships
nothing outscores one that ships a working app in terse messages. A case that declares
`expectations` is additionally graded on what it delivered.

```json
{
  "id": "todo-app",
  "persona": "...",
  "prompts": ["Build me a simple to-do list web app: ...", "Sounds good."],
  "expectations": {
    "outcome": "A working to-do list web app, delivered as a running Minds app tab...",
    "deliverable": {"kind": "minds-app"}
  }
}
```

- `outcome` (required) is the prose the outcome judge grades against -- the task description *for
  the eval*, alongside the prompts *for the agent*.
- `deliverable` is **required**. A block with none would lower to no programmatic checks, and
  rewardkit only pools a programmatic reward when criteria exist -- so the outcome dimension would
  silently become judge-only, carrying double the judge weight of every other case.
- `minds-app` is a **kind with implied checks**, not a hand-written check list: at least one
  *delivered* app registered in the workspace's `data/.state/apps.toml`, its supervisord service
  running, an HTTP 200 from each delivered app's root path, and the delivered repo captured as a git
  bundle. "Delivered" is narrower than "not a builtin" -- see below. Optional `min_registered_apps`, `http`, and `files` entries *refine* that set
  rather than replacing it. Unknown kinds and unknown keys are rejected at generation time.
- `test_commands` are run in the delivered repo and recorded for the judge, but never gated: gating
  them would punish cases whose prompts never mentioned tests.
- `ui_flows` are natural-language flows through the delivered UI, each with a verifiable end
  condition -- see [UI flows](#ui-flows). A flow may instead carry the reserved `script` field; that
  form is validated and carried but has no execution semantics yet, so it lowers to no check at all.
- `fresh_env` is reserved: parsed and carried, but nothing acts on it yet.

The kind is lowered into its explicit check list **once**, in the generator, and the lowered form is
written identically into `instruction.md` and `tests/case.json` -- which is what guarantees the
collector cannot probe a different set of checks than the judge scores. The authored form rides
alongside as `authored_expectations`.

**Evidence, not live state.** The verifier is a separate container that runs after the workspace has
been destroyed, so everything that needs the live app is captured at trial time into
`/logs/agent/verification/` (declared as a directory artifact) and the grade-time criteria score the
*record*:

```
verification/
  manifest.json          # the index: every probe with a typed status
  file_inventory.jsonl   # {path, size_bytes, mtime} per file (snapshot excludes + .git, 20k cap)
  apps.toml              # verbatim registry capture
  services.txt           # supervisorctl status output
  repo_state.json        # HEAD sha, the base and dwt-tip shas, commit count, git status --porcelain
  deliverable.bundle     # incremental `git bundle <clone HEAD>..HEAD` -- the agent's own commits
  http/<n>_<app>.json    # per probe: status, headers, timing, body head (256 KB cap)
  flows/<name>/log.jsonl # per UI-flow step: the verbatim page state, the action, the reasoning
  flows/<name>/step_NNN.png  # a screenshot per step
  trace.jsonl            # every bridge command the collector ran, failures included
```

Every manifest entry carries a status where **`failed` means the workspace fell short and `error`
means the harness could not find out** (the bridge died, a probe timed out). That distinction is
load-bearing: `error` entries are excluded from the criteria they would have fed (and when a whole
declared class is unmeasurable, `finalize.py` errors the trial rather than scoring it), so an agent
is never charged for a broken instrument. It cuts the other way too, and that is the harder half: a
workspace whose app registry exists and lists nothing is the agent shipping nothing, which must
score as `failed`, not be waved off as evidence the harness could not gather.

The registry/service/inventory capture runs for *every* trial
that got as far as a workspace, including cases with no expectations, which is what makes a
ships-nothing trial diagnosable; the expectation-driven probes are skipped on trials that never
finished, whose structural gates already zero the reward.

The harness probes the app **as delivered** and never starts it. Minds' promise to the client is a
running app tab, so "built it but never started or registered it" is a delivery failure, not
something for the harness to repair.

**What counts as a delivered app.** Not every registry row is one. Rows the registry marks
`internal = true` are machinery that forwards a port but has no page of its own to show -- the
owner-exec daemon, for instance, which answers 404 on `/` by design. A live trial confirmed this is
not hypothetical: counting it both inflated the delivered-app count and failed the implied root-path
probe, charging the agent for a daemon it never shipped.

A throwaway "isolated instance"
preview server registers through the same `forward_port.py` path and leaves its row behind when
abandoned, so counting it would both satisfy the app-registered check on something that was never the
deliverable and fail the root-path probe on its dead port. Those rows are excluded by reading the
instance runner's own state under `data/.state/isolated-instances/`, not by matching name patterns:
instance names are chosen by whoever starts them, so a pattern would miss arbitrary ones and wrongly
drop a real app named something like `recipes-test`.

Nor is a registry name a supervisord program name -- a multi-port app registers extra origin rows
(`<name>-admin`) that no program owns. The service-health check joins a row to its program through
the `forward_port.py` invocations inside each `[program:*]` block of `system/supervisord.conf`. A
delivered row that no program registers is recorded as `no_supervised_program`: the app was started
by hand and would not survive a restart.

The evidence directory is created at setup, before anything can fail, and is always declared as an
artifact even when empty: harbor records a missing declared artifact path as a failed entry and
refuses to regrade any trial carrying one, so a directory that only appeared when collection ran
would make every trial that died earlier permanently non-regradable.

The bundle's base is the eval-case commit the driver interposes (the template clone with
`system/vendor/mngr` overwritten), made with **fixed author and committer dates** so an identical
tree always yields the same sha. Without that the bundle could never be unbundled onto a regenerated
clone, which is the only reason to capture it; the evidence records that base sha and the template
tip it was built from so a replay can regenerate and verify the base.

`trace.jsonl` is the collector's own flight recorder: it exists so a `failed` verdict can be
attributed to the app rather than to the instrument, without re-running anything.

## UI flows

Liveness probes cannot see whether the app does what was asked -- a 200 with a stack-trace page
passes one. A `ui_flows` entry is a natural-language walk through the delivered UI with a verifiable
end condition, and it is the only level that checks the actual promise in the prompt:

```json
"ui_flows": [
  {
    "name": "persistence",
    "steps": "Open the app. Add a task named 'persist me'. Reload the page.",
    "expect": "'persist me' is still visible after the reload."
  }
]
```

A flow may also carry a `surface`. `origin` is the default and the only implemented one; the
reserved `minds-ui`, which would drive the Minds chrome and reach the app as an embedded iframe, is
rejected at generation time rather than silently falling back.

**The executor drives the app's forwarded origin from inside the box.** Flows run at the end of the
collection phase, inside its budget, in a headless Chromium the box launches for the flow -- its own
profile and its own CDP port, so no flow inherits another's cookies or storage -- navigating
`https://<label>.host-<hex>.localhost:8431/`, the exact URL the client's app tab iframes, served
by a `mngr forward` instance the driver owns. A host-side verification agent (the decider's sibling)
reads the page, decides one action, and a box-side step script performs it, screenshots the result
and reads the page back. The reasoning stays host-side, so the loop is budgeted, logged, and
attributable to harness spend.

This tests the app **through** the product's serving path -- forward proxy, SSH tunnel, label
origin, origin-scoped cookies -- rather than under it. Elements are addressed by ARIA role and
accessible name, taken from Playwright's `aria_snapshot`, which is also what the flow log records
verbatim for the judge.

**A step is one box exec.** Acting, screenshotting and re-reading the page are consecutive and need
nothing from the host in between, so they ride a single `environment.exec` -- and unlike the
previous executor, that exec is box-local, so the workspace hop is gone from the action path
entirely. Only the proxy's own tunnel touches the workspace.

The verification agent's spend is reported as `metadata.verifier_agent_usage`, beside
`decider_usage` and never folded into the agent's own cost fields. It runs on the decider's model by
default; `--ak verifier_model=...` overrides it. A flow's name must be unique within a case: it names
the flow's evidence directory.

**Grading a product with its own machinery cuts both ways**, so app failures and executor failures
are kept apart. An app that cannot satisfy the flow -- the `expect` does not hold, an element is not
there, the page never settles within the flow deadline, nothing was ever registered to open -- is
`failed` and counts against the agent. Machinery that could not be driven is `error`, with a reason
naming which layer went: `browser_launch_failed`, `cdp_connect_failed`, `forward_unreachable` (the
proxy itself), `tunnel_down` (proxy up, workspace leg dead), `tls_refused`, `step_bridge_failed`,
`host_id_unknown` (the workspace's host id could not be looked up, so no origin can be addressed),
and `verifier_agent_failed`.

The forward instance is the driver's own, not the one the headless minds backend may have spawned:
that gives it a port and a pre-auth token the driver minted, instead of a coupling to backend
internals and a cookie it never saw. It is configured at flag parity with minds' own spawn
(`forward_instance_test.py` asserts that against minds' argv builder, so the two cannot drift). It
adds one flag minds omits, a chosen `--port`, and drops the two that only shape how minds *embeds*
the app, which the origin surface has no analogue for.

**This executor replaced the workspace's browser fleet**, and the history is worth keeping. Flows
first shipped through `agentic-browser-fleet` and proved the concept live, but that shape coupled
the eval to the workspace's internal-tool security model -- the fleet's SSRF guard blocks every
delivered-app origin, so the eval only ran by changing the product -- and reached the app at a raw
in-container socket, leaving the forwarding path and everything cookie-shaped unverified. Flows now
run against dwt main with no product change required.

Scoring adds a third rewardkit dimension, `tests/outcome/`, present only for expectation cases (the
generator omits the directory otherwise, so rewardkit never emits a partial score for it). It holds
one programmatic criterion per declared class -- `app_registered`, `http_expectations_met`,
`files_expectations_met`, `ui_flows_completed` -- plus a `works_as_expected` likert judge over the
rendered expectations, the manifest, the conversation, and the flow evidence. The conversation is in
there deliberately: `DECIDE_FROM_PERSONA` turns are free-form, so a client who steers the build
mid-conversation must be graded against the evolved ask.

`ui_flows_completed` scores COMPLETION: the fraction of measurable flows that carried out their
declared steps. It does not score whether the app did what a flow's `expect` describes. That is the
judge's ruling, made from the step log and the screenshots, and having both sides rule on it would
put a trial-time judgement -- taken from less evidence, and frozen against regrade -- next to one
that can be revisited. Trial time collects; grade time verifies.

A grade-time pre-step (`render_flow_evidence.py`) flattens the flow evidence for the judge, because
rewardkit expands a listed directory exactly one level and never recurses. It always writes
`judge_flows_digest.txt` -- per flow: the declared steps, the `expect` the judge is to rule on, the
completion status, the agent's own description of the final page (evidence, not a verdict), then
every step's action, reasoning and page state -- and always creates a flat `judge_screenshots/`
holding each flow's last four frames, up to 24 in all, each under rewardkit's 1 MiB judge limit.
"Always" is load-bearing in both directions: rewardkit renders a listed path it cannot find as a
visible `[not found]` block, while an empty listed directory renders *nothing at all* -- which is why
the digest states the screenshot count instead of leaving the judge to infer it.

Reward composition changes only for expectation cases: `reward = gates_all_passed ? (0.5 * quality +
0.5 * outcome) : 0`. The 50/50 split says "a great app described badly and a great description of no
app are equally imperfect". It is a constant, not per-case configuration -- per-case weights would
make rewards incomparable across cases.

One new grading-infrastructure failure: an expectations case whose `state.json` says the conversation
finished but which produced no evidence bundle errors the trial rather than scoring 0. An absent
bundle on an unfinished or timed-out trial is expected and is not an error.

## Reward mapping

`quality = weighted mean(conciseness, nontechnical_language, proactive, wordiness guard)` -- likert
criteria normalized as `(raw - 1) / 9`, so raw judge scores stay recoverable (`raw = 9 * normalized
+ 1`; raw values are in `reward-details.json`). `reward` is that score (or an even split of it with
`outcome`, for expectation cases), zeroed unless every structural gate passed. The gate composition
lives in `tests/test.sh` (`finalize.py`) because rewardkit's `reward.toml` aggregations cannot
express "binary gate zeroes a weighted mean"; all judging and scoring happens inside rewardkit.

Note how rewardkit weights a dimension, because it is easy to get backwards: every `.py` criterion
in a dimension directory is averaged into **one** programmatic reward of weight 1.0, and each
`judge.toml` is a **second** reward carrying its own `[judge].weight`. So the quality judge's
`weight = 3.0` buys equal weight *per criterion* across its three judge criteria and the one
programmatic guard, while the outcome judge's `weight = 1.0` is what makes it exactly half its
dimension however many programmatic criteria the case declares.

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
