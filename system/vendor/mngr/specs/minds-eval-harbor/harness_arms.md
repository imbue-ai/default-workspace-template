# Harness and model arms

## Purpose and scope

This spec lets one minds_evals run drive every case of a dataset, from workspace creation to grading, on a chosen harness and model.
The harness is chosen by the provider lane the workspace is signed in on, because that is how the product itself decides it; the model, effort and speed tier are then set through the product's own model endpoint before the first turn.
The primary deliverable is the local run: `just minds-evals-run` with a handful of `--ak` flags.
A secondary section describes how the same flags become a matrix of arms -- pair times harness config -- in the scheduled CI workflow.
The audience is the engineer implementing the driver change and whoever maintains the CI workflow.
Background, the product facts this design rests on, and the measurements behind them are recorded in imbue-ai/mngr-internal issue #712.

Out of scope: codex (its lane cannot be signed in without a human), a metering proxy for non-Anthropic lanes, setting the model at create time (a workspace-template feature, see "Follow-ups"), and per-case arms.

## Background

Four facts about the workspace template (default-workspace-template `main@112ee09b4`) shape the design.

- **The account decides the harness.** `POST /api/agents/create-chat` takes `name`, `account_id`, `agent_id` and `message` only; the chat runs on the harness of the lane its account was minted on.
  Lanes that can be minted without a human today: `anthropic` (claude), and `api-key`, `openrouter` and `opencode-go` (all pi-coding).
  The `openai` lane (codex) needs a device-auth flow on a PTY, so it is out of scope.
- **Model, effort and speed are set after create.** `POST /api/agents/<id>/model {model_id, effort, fast, axes}` is harness-blind and validated against the agent's catalog.
  `effort` is required whenever the model has an effort axis, even for a model-only change.
  There is no HTTP readback of the live choice; the loopback `GET /api/agents` lists `id`, `name` and `state` only.
- **The first chat always greets on the default model in fast mode.** The `first` create template is stacked server-side on the first chat created with an empty `message`, delivers `/welcome`, and turns fast mode on for claude and codex.
  Nothing a client sends can decline it, and a create-template setting outranks any settings file in the workspace clone.
- **Measured on 2026-09-08** with one-turn probe trials: the post-create switch works on claude and pi-coding and adds no step to the captured ATIF `trajectory.json`; pi-coding completes a case end to end on the `api-key` lane with an Anthropic key and on the `openrouter` lane (default model there: `moonshotai/kimi-k2.6`, which the pricing table does not know, so such a trial reports `cost_usd: null` and names the model under `unpriced_models`), with post-welcome switches to `openrouter/~google/gemini-flash-latest` and `openrouter/~openai/gpt-mini-latest` both applied and answered on the switched model; and the greeting is most of a trivial case's spend.
  pi reports the model as the tag minus its `openrouter/` prefix, so a harness config that wants a priced trial names a concrete id the pricing table knows (`openrouter/openai/gpt-5-mini` reports as `openai/gpt-5-mini`), not a `~vendor/name-latest` alias.
  The agent's live `model_state.json` (`{model, effort, fast}`, written by the harness's statusline or lifecycle extension) is readable through the bridged exec at `$MNGR_HOST_DIR/agents/<chat_id>/model_state.json` (`/mngr/agents/...` in the workspace), even though no HTTP endpoint serves it.

The driver today ([concise.md](concise.md), `driver.py::_prepare_workspace`) signs in through `/api/claude-auth/submit-credentials`, creates the chat against the returned account, waits for the welcome to be answered, and sends turn 1.
Everything in this spec happens inside that sequence.

## Goals

- One run drives every case of a dataset on one harness, one model, one effort and one speed tier, chosen on the command line.
- The dataset is unchanged: `environment/` stays byte-identical, so every harness config of a pair shares one image build.
- The graded transcript is the same shape it is today; the control messages that apply the harness config never reach a judge.
- Every trial records what was requested and what was observed, so a trial that silently ran on the wrong model is visible.
- A run that cannot honour its harness config fails before it spends a turn, with a reason that names it.

## Non-goals

- Making the greeting run on the harness config's model.
  The welcome runs on the workspace's default before any switch can happen; this spec attributes it rather than avoiding it.
- Harness-aware grading.
  The verifier's harness-quality dimension is claude-shaped; this spec only stops it from mis-scoring other harnesses.
- A proxy path for non-Anthropic lanes; those trials are priced from the transcript.
- Choosing which arms the CI runs; that decision is recorded in the harness config file, not here.

## Design

### The arm and its harness config

An arm is the whole treatment a trial ran under: the (mngr, dwt) pair its box and workspace were built from, together with the **harness config** -- the harness, model, effort and speed tier the chat drives on.
Both halves are run-level, never per-case: every case of a dataset runs on the same pair and the same harness config.
The pair is fixed when the dataset is generated; the harness config is what this spec adds.
It rides in on harbor agent kwargs through the `just minds-evals-run <dataset> <job> <concurrency> <harbor_args>` seam, and is parsed in `MindsPersonaDriver.__init__` like `proxy` and `snapshot_mode` are today.

| kwarg | meaning | default |
|---|---|---|
| `--ak lane=<id>` | the provider lane to sign the workspace in on: `anthropic`, `api-key`, `openrouter`, `opencode-go` | `anthropic` |

"Lane" is the workspace template's own term, not one coined here: its chat app defines a lane as an AI provider reached through a particular harness (`system/apps/chat/imbue/chat/harnesses/lanes.py`), the accounts API takes it as `lane_id`, and the UI shows lanes under the label "provider".
The kwarg keeps the wire name so the spec, the driver and the template's API all say the same word.
It is not an authentication method: each lane lists its own sign-in methods (`api_key`, device auth), and the driver always uses `api_key`.
| `--ak key_provider=<id>` | for the `api-key` lane only, which provider the key belongs to (`anthropic`, `openai`, `openrouter`, ...) | required on `api-key`, rejected elsewhere |
| `--ak key_env=<VAR>` | the environment variable holding the lane's key | derived, see "Credentials" |
| `--ak model=<id>` | the catalog id to switch the chat to before turn 1 | unset: no switch |
| `--ak effort=<level>` | the effort or thinking level to set with the model | required with `model` |
| `--ak fast=<bool>` | the speed tier to set with the model | `false` when `model` is given; without `model` there is no switch and the template's tier stands |

`model` and `effort` come together: the endpoint refuses one without the other, so the driver refuses the run at construction rather than five minutes in.
`fast` without `model` is also refused, for the same reason: the endpoint needs a model id to apply any axis, and the driver has no readback to fill one in.

**The default harness config** is a lane with its credentials and nothing else: no `model`, no `effort`, no `fast`.
It makes no switch and changes no setting of the workspace; the chat runs exactly as the product ships it, which for the first chat on claude means the pinned model in fast mode, and on pi-coding the provider's default at standard speed.
It still records everything the recording section describes (the harness from the accounts listing, the observed models from the transcript), it just requests nothing.
A run with no harness flags at all is the default harness config on the `anthropic` lane, on whichever pair the dataset was generated from.
A run line with no harness flags, and the scheduled CI's `default` cell, are that arm, byte-for-byte the product as shipped.

**Every other checked-in harness config** names `model` and `effort` and leaves `fast` at `false`.
Fast mode doubles the rate and changes nothing else, so configs compared on cost must all run standard; the default harness config is the one exception, and its cost is read as the product's cost, not as a point on that comparison.

Model ids are the workspace's catalog ids, not API model names, and they differ by harness.
claude offers `fable[1m]`, `opus[1m]`, `sonnet[1m]` and `haiku` with efforts `low`, `medium`, `high`, `xhigh`, `max`.
pi-coding offers `<provider>/<model>` tags gated by the account's key, for example `anthropic/claude-haiku-4-5` or `openrouter/<vendor>/<model>`, with thinking levels `off` through `max` per model.
The driver validates nothing about these strings itself; the endpoint's 400 is the validation, and the driver reports its detail, clipped to 300 characters.

Examples:

```bash
# the default harness config on the anthropic lane: what every run is today
just minds-evals-run $DS default 3
# opus at standard speed, for cost comparisons against the other arms
just minds-evals-run $DS opus-standard 3 --ak model='opus[1m]' --ak effort=high
# cheap claude config
just minds-evals-run $DS haiku 3 --ak model=haiku --ak effort=medium
# pi-coding on an Anthropic key
just minds-evals-run $DS pi-haiku 3 --ak lane=api-key --ak key_provider=anthropic \
  --ak model=anthropic/claude-haiku-4-5 --ak effort=medium
# pi-coding on OpenRouter
OPENROUTER_API_KEY=... just minds-evals-run $DS pi-gpt-5-mini 3 --ak lane=openrouter \
  --ak model=openrouter/openai/gpt-5-mini --ak effort=medium
```

### Credentials

The decider, the judges and the UI-flow agent keep using `ANTHROPIC_API_KEY`; that is the harness's own spend and does not change with the arm.
The workspace's key is a separate concern, because the lane decides which provider it must belong to.

The driver reads the workspace key from the variable named by `key_env`.
The default is `ANTHROPIC_API_KEY` on the `anthropic` lane, `OPENROUTER_API_KEY` on the `openrouter` lane, and `<KEY_PROVIDER>_API_KEY` on the `api-key` lane, upper-cased with dashes turned into underscores (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`).
The derivation is a convenience, not a contract with the template: the template's own table names the variable per provider and does not always follow the pattern (`google` reads `GEMINI_API_KEY`), and the `opencode-go` lane has no derived default.
Those two cases are run with an explicit `key_env`.
A missing variable fails the run at construction with a message naming the variable and the lane; a lane outside the table is refused there as well, and no `key_env` makes one runnable.

The `just minds-evals-run` recipe keeps requiring `ANTHROPIC_API_KEY` (the decider and judges need it) and does not learn about the other variables; the driver's own check covers them.

### Workspace preparation

The sequence in `_prepare_workspace` gains one branch and one step.

1. Create the workspace (unchanged).
2. Start the proxy if enabled (unchanged; see "Proxy interaction").
3. **Sign in by lane.**
   On the `anthropic` lane the driver keeps posting to `/api/claude-auth/submit-credentials`, the path it uses today, because its answer carries `auth_mode` and the driver verifies a proxied sign-in by it.
   The accounts flow could carry that lane too: its `api_key` method parses the same `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` lines (`auth_flows.py::claude_env_from_paste`), so the base URL is not what separates the two paths, only the readback is.
   **Decision:** the two paths stay separate until a metering proxy is wanted on another lane; that is the moment to give the accounts flow what the proxy needs (see "Follow-ups") and retire the split.
   On every other lane it uses the accounts flow: `POST /api/accounts {lane_id, method_id: "api_key"}` returns a `flow_id`; `POST /api/accounts/flow/<flow_id> {api_key, key_provider?}` returns `{state, detail, account_id}`.
   `key_provider` is sent only on the `api-key` lane.
   The template probes the harness with the key before it answers, so a `state` of `failed` means the key does not work for that provider and the trial stops there with the `detail`.
   A `pending` answer is polled on the same URL until it settles or the readiness deadline runs out.
   Both paths end in the same `WorkspaceAuthentication` value, so the rest of the sequence does not know which lane signed in.
4. Read `GET /api/accounts` once and record the minted account's `harness`; this is the driver's only readback of the harness, and it comes from the template, not from the kwargs.
   The row's `lane` is read in the same pass and logged as a warning when it is not the lane the run asked for; the harness config keeps the requested lane, since the harness is what grading and the record need.
5. Create the chat against the account and wait for it to reach `WAITING` and to answer its welcome (unchanged).
6. **Apply the harness config.**
   If the run names a `model`: `POST /api/agents/<chat_id>/model {model_id, effort, fast, axes: ["model", "effort", "fast"]}`.
   All three axes are always sent, so the endpoint applies all three regardless of what the frontend's diffing rule would have considered changed.
   Then wait for the chat to be `WAITING` again, with the same poll the welcome wait uses; on claude the switch is three slash commands typed into the session and the agent is briefly busy answering them.
7. Capture the pre-turn registrations and continue into the conversation (unchanged).

A stepped case prepares its workspace once, on its first step, so the harness config is applied once and every step's `state.json` names the same arm; the observed half is read off the transcript each step captured, so it is that step's own.

The harness is already the arm's before the welcome: the chat is created against the account the lane minted, which is the same up-front choice the product's own chat chooser offers, so the welcome runs on the arm's harness.
Only the model is decided after creation, because create-chat carries no model field and the product exposes the choice solely through the post-create model endpoint.
The switch happens after the welcome, not before it, because the welcome is delivered by the create template the moment the agent is ready, and a switch typed into that window would race it.

### Failure modes

The two construction rows below refuse the whole run before any trial exists, so they leave no `state.json` at all.
Every other failure marks the trial `timed_out` with a `timed_out_reason` that names the arm, in the same style as today's preparation failures, so `check-run` and the diagnosis reference read it like any other reason.

| failure | when | reason text |
|---|---|---|
| key variable unset | construction | run refused, no trial: `no <VAR> to sign the workspace in on lane <lane>` |
| `model` without `effort`, `effort` or `fast` without `model`, `key_provider` off the `api-key` lane or missing on it, an unknown lane | construction | run refused |
| accounts flow does not start (non-2xx) | step 3 | `the workspace refused to start a sign-in on lane <lane>: <detail>` |
| flow settles `failed` | step 3 | `the workspace rejected the key for lane <lane>: <detail>` |
| flow still `pending` at the deadline | step 3 | readiness reason, as for the other waits |
| switch answers 400 | step 6 | `the workspace refused the requested model choice: <detail>` |
| switch answers anything else that is not 2xx (500, 0, 401, ...) | step 6 | `the workspace could not apply the requested model choice: <detail>` |
| chat not `WAITING` after the switch | step 6 | readiness reason: `the workspace chat never settled after the model switch` |

A 400 is a configuration error and never a workspace fault; the run should stop rather than retry, and the reason quotes the endpoint so a typo in a catalog id is legible from the trial listing.

### Recording

`state.json` gains an `arm` block and the trajectory's `extra.minds_evals` gains the same block, so both the driver's record and the graded document say what treatment the trial ran under:

```json
"arm": {
  "mngr_sha": "c276277e...",
  "dwt_sha": "bdf38915...",
  "harness_config": {
    "lane": "api-key",
    "key_provider": "anthropic",
    "account_id": "9338aed1...",
    "harness": "pi-coding",
    "model": "anthropic/claude-haiku-4-5",
    "effort": "medium",
    "fast": false,
    "model_choice_switch": "applied",
    "observed_models": ["claude-haiku-4-5"],
    "welcome_model": "claude-opus-4-8",
    "is_model_confirmed": true
  }
}
```

`mngr_sha` and `dwt_sha` repeat the top-level keys of `state.json` and of the trial metadata, which keep their place; the block carries them so it names the whole treatment on its own, and a reader holding one arm block needs nothing else to say what it was.

- `harness` comes from the accounts listing in step 4; `model_choice_switch` is `applied`, `skipped` (no `model`), or the failure that stopped the trial, and is named for the single call that sets model, effort and fast together (`POST /api/agents/<id>/model`).
- `observed_models` is the set of `model_name` values on the ATIF agent steps after the client's first turn, and `welcome_model` the one on the greeting step.
  A step filed under the `<synthetic>` pseudo-model counts towards neither: no inference answered it, and counting it would read a switched trial as one that ran on two models.
  The two halves are separated at the step carrying the driver's own first message: turn 1 is the first `user` step whose stripped message is that text, and everything before it belongs to the greeting, so the greeting falls on the greeting side whether the harness filed it as a `user` step (pi) or a `system` step (claude), without either harness's greeting text being spelled out anywhere.
  Both are read straight out of the captured document, so they are available on every harness -- a document that was captured but did not validate fills them in even though the trajectory beside them is the hand-built one.
  They go empty together, and a trial that captured no document is not the only one that leaves them so: a captured document with no step matching that first message (its shape changed, or the trial never sent a turn) records neither, logs a warning, and leaves `is_model_confirmed` `null` as any other silence does.
- `is_model_confirmed` is `true` when `observed_models` is exactly one model and it matches the requested catalog id per the harness's naming (`haiku` reports as `claude-haiku-4-5-20251001`, `anthropic/claude-haiku-4-5` reports as `claude-haiku-4-5`).
  The matching table is small and lives with the harness config parsing; an id it does not know leaves the field `null`, never `false`.
- On a proxied `anthropic` trial the proxy's `per_model` remains the ground truth and the confirmation is computed from it instead.

`usage.json` is unchanged in shape.
The greeting is already its own `per_model` row when it ran on a different model than the turns, and `welcome_model` says which row that is; a reader comparing arms subtracts it.
Nothing in this spec changes the cost fields harbor reads.

### Grading on non-claude harnesses

The verifier's `harness_quality` dimension scores a report built by claude-shaped rules: a skill invocation is recognised as the `Skill` tool, and two of the six failure signatures name claude's own skill and plugin vocabulary -- which is what the judge prompt spends its weight on.
On a pi-coding trajectory those rules find nothing, so the report is thin because it was built thin rather than because the harness held, and a prompt that reads an empty report as a sound harness returns 1.0: a false pass rather than a measurement.
The remaining signatures are shell-level and harness-blind (`ModuleNotFoundError`, `command not found`, `Exit code 127`), so the reports are still worth writing on every harness; they are simply not worth two opus judges and a fifth of the reward.

The verifier reads the harness from the trajectory's `agent.name` (`claude`, `pi-coding`), which mngr's transcript already sets.
A trajectory that names none falls back to the `harness` the arm block recorded, and then to `claude`: the driver's hand-built fallback names the *driver* in `agent.name`, and mngr writes `unknown` when it cannot resolve the agent, so a claude trial that merely lost its transcript must not lose a dimension over it.
For a harness other than `claude`:

- `harness_quality` is not scored: its judge is not called. Every trial's `reward-details.json` carries a `harness` block naming what ran and whether the dimension applied, so a dimension missing from the scores reads as one that does not apply here rather than one that failed to emit.
- The reward composition drops the harness share: `reward = gates_all_passed ? earned : 0` instead of `0.8 * earned + 0.2 * harness_quality`.
  This keeps `reward` in the same range and the same meaning on every arm, at the cost of a claude arm and a pi arm weighting quality differently; that is acceptable because arms are compared within a harness first.
- The judge transcript is rendered as it is today; lowercase `bash` calls render as tool calls the judges can see because the renderer keys on the step's `tool_calls`, and only the executing-tool marker is missed.
  Extending the executing-tool set with `bash` is a one-line change and part of this spec.

`usage.py`'s delegation detection is also claude-shaped (`Agent`/`Task` tool names and the `create_worker.py launch` command marker).
On pi-coding the launch marker still fires, because the launch-task skill is shared across harnesses, and pi has no subagent tool, so `is_cost_complete` stays truthful.
No change there.

### Proxy interaction

`--ak proxy=true` stays an `anthropic`-lane feature.
The proxy's address only reaches the workspace through the `ANTHROPIC_BASE_URL` line of the claude sign-in, and its model list is Anthropic-only.
On any other lane the driver refuses `proxy=true` at construction, with a message saying the proxy meters the anthropic lane only, rather than starting a proxy nothing will call.
Non-Anthropic trials are priced from the transcript, which `usage.py` already does; their `is_speed_observed` stays `false` and their `is_cost_complete` follows the transcript rules.

### Comparability

An arm changes the system under test, in either half.
A run with `fast=false` is not comparable to a run that left the template's fast mode on, and a pi-coding run is not comparable to a claude run on any dimension but cost.
A comparison reads only when the two arms differ in one thing: two that differ in both their pair and their harness config attribute nothing to either.
Result sets are versioned or flagged at the arm, the same way [goal_driven_turns.md](goal_driven_turns.md) flags the adoption of a goal entry.

## Local runs

A local run on a chosen harness config is the existing run recipe plus the flags above; nothing else changes.

```bash
just minds-evals-generate apps/minds_evals/configs/eval-config-small.json /tmp/minds-evals/datasets/small
just minds-evals-run /tmp/minds-evals/datasets/small small-haiku 3 --ak model=haiku --ak effort=medium
OPENROUTER_API_KEY=... just minds-evals-run /tmp/minds-evals/datasets/small small-pi 3 \
  --ak lane=openrouter --ak model='openrouter/<vendor>/<model>' --ak effort=medium
```

The dataset is generated once and shared by every harness config run against it; harbor refuses to reuse a job name, so each run gets its own.
The README's usage section gains the kwarg table and one example per lane, and its diagnosis section gains the new `timed_out_reason` texts.

## CI integration

The scheduled workflow `.github/workflows/minds-evals-scheduled.yml` evaluates a **matrix of arms**: each frozen (mngr, dwt) pair times each selected harness config.
A cell is one arm, and two cells that differ in both halves attribute nothing to either, so a matrix is worth reading only where it varies one thing at a time.

### The harness config file

`apps/minds_evals/configs/harness_configs.json` holds the named configs, as `{"harness_configs": [{"name": "haiku", "is_nightly": true, "lane": "anthropic", "model": "haiku", "effort": "medium"}, ...]}`.
Only `name` and `is_nightly` are required; every other field is one of the run line's own kwargs from the table above and carries the same default an unset kwarg has, so an entry that names nothing beyond its lane is the default harness config.
A name matches `^[a-z0-9][a-z0-9.-]*$`, is at most 30 characters, is unique in the file, and is never `oracle`, because it labels a job, a concurrency group, an artifact and a line of the report, beside the oracle's own.
Dots are in the pattern because a model version is part of what names an arm (`pi-glm-4.7-flash`), and a job name, a concurrency group, an artifact name and a cache key all take one.
The checked-in entries are, in file order, `default` (the `anthropic` lane and nothing else: the product exactly as it ships), `haiku` (`haiku`, effort `medium`), `pi-haiku` (the `api-key` lane on an `anthropic` key, `anthropic/claude-haiku-4-5`, effort `medium`), `pi-gpt-5-mini` (the `openrouter` lane, `openrouter/openai/gpt-5-mini`, effort `medium`), `pi-glm-4.7-flash` (the `openrouter` lane, `openrouter/z-ai/glm-4.7-flash`, effort `medium`) and `opus-standard` (`opus[1m]`, effort `high`).
`default`, `haiku` and `pi-gpt-5-mini` are nightly; the rest run only when a dispatch names them.
The file's order is the order the cells are decided in and so the order of the report's grid columns, which is why arms worth reading against each other -- `haiku` beside `pi-haiku` -- are listed side by side.
Which configs run nightly is a spend decision, and `is_nightly` is where it is recorded: the file carries the decision rather than this spec, and a config the file lists but no night selects costs nothing to keep.
Every entry is validated through the driver's own `parse_harness_config` on the free `resolve` job, selected or not, so a config the driver would refuse at construction fails before any paid runner starts; a unit test validates the checked-in file on every test run.

The `workflow_dispatch` input `harness_configs` takes comma-separated names.
Empty is the default, and is what a schedule and a `minds-evals-run/**` push get: it selects the nightly set rather than every config in the file, which is what makes a non-nightly entry free to check in.
An unknown name fails the `resolve` job.

### The jobs

- `resolve` (free) freezes each pair's refs to SHAs, lists the green markers once with `gh cache list --key minds-evals-green-`, and runs `minds-evals ci-matrix` to decide the cells: which pairs run, which cells run, and what each cell's run line and marker key are.
- `oracle` is a matrix over the pairs with at least one running cell.
  Each entry generates the dataset, runs the oracle pass, checks it, runs the Modal environment cleanup (the named deletion and the 8 h backstop sweep), and uploads `minds-evals-summary-<pair>` and `minds-evals-jobs-<pair>-oracle-<run_id>`.
  The oracle pass boots no workspace, so it is independent of the harness config and runs once per pair rather than once per cell; a pair's live cells run only after its oracle passed.
- `evaluate` is a matrix over the running cells.
  A cell fetches only its own secrets, downloads its pair's oracle summary and refuses to start the live pass unless that oracle passed, regenerates the same dataset at the same SHAs (so the image build is a Modal cache hit), runs `just minds-evals-run` with its harness config's `--ak` flags appended, checks it, deletes the environments its own job recorded, uploads `minds-evals-summary-<pair>-<config>` and `minds-evals-jobs-<pair>-<config>-<run_id>`, and writes its green marker on success.
- `notify` posts one Slack message per pair through `minds-evals ci-report`, and writes the same reports into the run summary.

The oracle job's concurrency group is `minds-evals-oracle-<pair>` and a cell's is `minds-evals-<pair>-<config>`; the harbor job names are `<pair>-oracle-<run_id>` and `<pair>-<config>-live-<run_id>`.

### Secrets and green markers

Every job that runs a pass -- the oracle and the cells -- fetches `mngr/ci/ANTHROPIC_API_KEY` (the judges spend it on every pass and the decider on every live one) and the Modal token pair; `resolve` fetches nothing and `notify` only the Slack webhook.
A cell additionally fetches `mngr/ci/<key_env>` when its config's `key_env` is not `ANTHROPIC_API_KEY`, which is how the `pi-gpt-5-mini` cell gets `mngr/ci/OPENROUTER_API_KEY`.
A lane's key is never exported into a cell that does not sign in on that lane.
A `key_env` with no Vault secret behind it fails that cell at the fetch step, before a box is built, and the driver's construction-time check is the backstop for a runner where the fetch succeeded but the variable is empty; either way it is the cell that fails, not the run.

A cell that passes end to end is recorded green under the cache key `minds-evals-green-<pair>-mngr-<mngr sha>-dwt-<dwt sha>-cfg-<config path slug>-hc-<harness config name>-<12-hex digest>`.
The digest is over the parsed harness config's kwargs rather than the harness config file's path, so editing one config re-runs its own cells under the same name while leaving the others green.
A cell is skipped when its exact key is among the markers the run may restore -- those saved on the run's own ref or on the default branch -- and `force` runs it anyway.
A pair whose every cell is skipped runs no oracle pass either.
A key with no `-hc-` segment names no cell, so a marker written before the matrix existed is never read as green.
The lookup is one `gh cache list` call rather than one `actions/cache/restore` step per cell, because that action cannot be looped over a matrix decided at run time.

### What the run reports

`check-run` needs nothing from the matrix: it fails a trial whose harness config named a `model` and whose `is_model_confirmed` is `false`, with a reason naming the requested model and the ones observed, so a cell goes red on a switch that did not take, while `null` is neutral and a config that named no model is not judged on it.
Its summary's arm column names the cell's harness half, with the pair in the SHA columns beside it.

`minds-evals ci-report` renders one Slack message per pair -- the two pairs answer different questions, and a reader acts on one of them at a time -- and writes them as a JSON array of webhook payloads, one per message.
Only the blocks an incoming webhook accepts are used: a webhook refuses `data_table` outright (a minimal one is answered with `400 invalid_blocks`), so the sorting and paging it would bring are out of reach until the notify job posts as an app with a bot token, and `markdown` is refused as well; `table` and `container` are what the layout is built from.
A message is a header (`minds-evals: <pair> -- <verdict>`, the verdict being the worst of the pair's oracle pass and its cells), a section carrying the pair's label and oracle verdict, the run's duration and trigger, and the `(oracle only)` and red-job notes where they apply, a `table` block holding the grid with a context line under it legending its colours, a `*failed trials*` section and `table` block where something failed, a collapsed `container` holding the pair's judge scores, a details section, and a context line linking the run's logs and artifacts.
The message posts under `:big_brain:` when every arm it reports came out green -- every cell passed, every cell was skipped as already green, or an oracle-only run's oracle passed -- and under `:brainless:` for anything else, the undecided-run message included; the choice is read off the same verdict the header spells out, so the avatar and the words cannot disagree, and a run whose arms all passed but whose job went red keeps the green one because the icon answers "are the arms good?".

The grid is one column per harness config in matrix order -- or a single `oracle` column on an oracle-only run -- and one row per case.
A cell is a `rich_text` cell placing the trial's reward on the absolute 0..1 scale as a coloured square (red under 0.25, orange under 0.50, yellow under 0.75, green at or above it), then the reward, then a bold cross where the trial did not pass (incomplete, gates failed, unmeasured evidence, wrong model) or a spacer of the same width where it did.
The colour therefore says one thing throughout and the verdict never competes with it; the context line under the grid states the bands and the cross, derived from the bands themselves so the legend cannot drift from the cells.
`:heavy_minus_sign:` is a pass that was never attempted (skipped because it is already green, or gated off by a failed oracle) and `:grey_question:` a cell with no reward to place: a broken pass, a case that column has no trial for, or a trial that was never graded.
The header row and the case column stay `raw_text`, and a pair that graded nothing -- skipped, unresolved, or every cell broken -- has no grid at all.

The failures table lists every graded trial that did not pass, in column order, as the harness config, the case and the reason `check-run` recorded (a wrong-model reason, unmeasured evidence ids, gates failed, or `did not complete (...)`).
It is drawn only when something failed: a heading over an empty table reads as a measurement that went missing rather than as a night with nothing to report.

The container, titled `judge scores`, is collapsed by default and holds a context line and one table for the whole pair rather than one table per harness config, so a criterion can be compared straight down its own column and the message does not grow two blocks per config.
Its rows are every graded trial of the pair: the harness config, the case, the reward to two decimals, each criterion's raw likert answer as an integer, and what became of the trial.
The criteria are the union across every arm in first-seen order, so a criterion only one config was scored on still gets a column and the arms not scored on it print `-` rather than a zero.
A heading is the bare criterion name, and `<dimension>: <criterion>` only where two dimensions scored criteria of the same name; the context line above the table states which dimension scored which criteria once instead.

Slack refuses the whole message over 20 cells in a table row or 10,000 characters across the cells of all its tables, so the message holds itself under both.
Every cell is clamped to 120 characters first, a case id and an incompletion reason being unbounded; then a pair scored on more than 16 criteria keeps the first 16; then the judge table, the only part that grows with cases times configs times criteria, is cut to whole rows of what the grid and the failures table left of the character budget.
Whole rows, because a row cut in half would line its scores up under the wrong headings.
Each cut says so out loud: in the context line above the table, or, where no row fits at all, in a context block where the container would have gone.

The details section is left with what belongs to no arm's own row: one line per skipped, broken or not-evaluated cell, the failing trials of a pass with no grid column of its own (a failed oracle on a pair whose cells ran), and every passing trial whose requested model nothing confirmed.
It is budgeted at 2900 characters and cut on a line boundary.
A run that resolved nothing gets a single `minds-evals -- broken` message naming the three job results instead of pairs.
Every message also carries a plain mrkdwn `text` saying the same thing with the grid, the failures and the judge table as fixed-width fences, which is what `notify` writes into the step summary and what it re-posts on its own, with a `::warning::`, when Slack refuses a payload's blocks.
The grid's fence keeps the words `ok`, `FAIL`, `-` and `?` in place of the emoji, because Slack renders no emoji inside a fence, and the judge fence carries the same dimension legend on the line above it.
The report never fails the run: an unreadable matrix or a missing summary is reported as such and the exit code is zero.

Testing the workflow before it is on the default branch goes through a push to `minds-evals-run/<anything>`: the main pair, the nightly configs, and the oracle pass only unless the head commit message carries `[live]`.
A push and a run that stops at the oracle both decide every cell as if `force` were set, because the green markers would otherwise skip the whole matrix on a night when nothing moved and leave the run with nothing to exercise -- an oracle-only run with no oracle pass, a `[live]` push with no live pass.
An oracle-only run writes no marker of its own, the marker step being in the live pass it never reaches, and a push writes its markers under its own ref, which a nightly on the default branch cannot restore.

Cost is one box per case per running cell.

## Testing

- Unit tests for the kwarg parsing (each refusal above), the `key_env` derivation, the sign-in dispatch by lane, the switch payload, and the arm block written to `state.json` and the trajectory.
- Unit tests for the observed-model confirmation table, including an unknown id leaving the field `null`.
- A unit test that loads the checked-in `configs/harness_configs.json` and validates every entry through the driver's own kwarg parsing, so a config that would be refused at construction is caught by the test run rather than by a night's `resolve` job.
- Unit tests for `ci-matrix`: the selection an empty `--select` makes, an unknown name refused, the marker key composed for a cell, a cell skipped on a matching marker and run under `--force`, a marker on a ref the run cannot restore ignored, and a pair with no running cell dropped from the oracle matrix.
- Unit tests for `ci-report`: a message per pair, a header verdict for each of passed, failed, skipped, not evaluated and broken, the icon each of those posts under, the grid marks for a graded, a never-attempted and a broken cell, a failures-table row carrying a wrong-model reason and a details line naming an unconfirmed model, the judge table's dimension-qualified headings and its dash for an unscored criterion, the criteria dropped from a table that would exceed a Slack row, the rows dropped from one that would exceed the message's character budget, a truncated details block, and a matrix that cannot be read still producing a message.
- Unit tests for the verifier's not-applicable path: a pi-coding `agent.name` drops the harness share and records the dimension as not applicable; a claude one is unchanged.
- A live probe run, by hand, of a one-turn case (a checked-in `configs/eval-config-probe.json`, one literal prompt, no expectations) on the `api-key` lane with an Anthropic key, reading `arm.harness_config.harness == "pi-coding"`, `model_choice_switch == "applied"` and `is_model_confirmed` off the trial's `state.json`.
  It stays a run rather than a test: every case here boots a Modal box and spends real money, this app has no live suite to put such a test in, and the claude path is covered by the existing runs.

## Follow-ups

- **Model at create time** (workspace template): a `model_id`, `effort`, `fast` triple on `create-chat`, validated against the account's harness catalog and turned into each harness's create-time settings, would let the greeting run on the harness config's model and remove step 6.
  It is the same design as the account binding extended one step, and it belongs with the codex work.
- **Codex**: an API-key method on the `openai` lane is the template change that unblocks it; the driver then needs nothing new beyond an entry in the lane table.
- **Proxy for other lanes**: OpenAI entries in the LiteLLM model list and a base URL for pi or codex; none of it is a LiteLLM limitation.
  The accounts flow is where the endpoint belongs: the account already pairs a credential with a provider, and a `base_url` on the submit step would let each lane's writer place it where its harness reads it (claude's env lines, pi's per-provider model config, codex's `model_providers`).
  That is a workspace-template change, and it is also the point at which the driver's `anthropic`-lane split above can be retired, once the flow reports enough to verify a proxied sign-in.
- **Harness-aware harness quality**: signatures per harness, read from the ATIF tool calls; whether the ATIF carries enough for pi is the open question.
- **Pre-turn readback from `model_state.json`**: reading the file through the bridged exec right after the switch would confirm the harness config's model before turn 1, on every harness, instead of after the transcript is captured; it is the same file the product's model bar reads.

## Open questions

None at the moment.
The effort question was measured on 2026-09-08: an unswitched claude chat's `model_state.json` reads `{"model": "claude-opus-5[1m]", "effort": "high", "fast": true}`, so the `opus-standard` config (`model=opus[1m] effort=high fast=false`) differs from the default harness config in speed only.
