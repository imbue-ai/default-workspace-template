# minds-evals

Harbor-based Minds persona evals. Each persona case in an eval config becomes one
[harbor](https://github.com/harbor-framework/harbor) task; a run drives real multi-turn
conversations against real Minds workspaces on Modal and grades the transcripts with a rewardkit
verifier. It replaces a bespoke pre-harbor harness; every config that harness accepted still reads
unchanged, and the schema is a superset of it: a config that adopts a goal entry (below) runs here
only.

## How a trial works

1. The task's environment is a **box**: a full Minds computer (the adapted box Dockerfile plus a
   staged shallow clone of mngr-internal at an exact SHA), built on Modal's builders as a single
   image per mngr SHA and cached whole (~2.5 minutes cold, seconds on a hit).
2. The **driver** (`MindsPersonaDriver`, a host-side harbor agent) starts the Minds backend inside
   the box with per-trial env: the Modal token pair parsed from your `~/.modal.toml` and a salted
   per-trial `MNGR__PROVIDERS__MODAL__USER_ID` scope. No AI credentials go in that env.
3. The driver creates one **nested workspace** through the production path (Minds API ->
   `mngr create` -> Modal provider) and then **signs it in the way a user does**, by posting the
   credentials to the workspace's own `/api/claude-auth/submit-credentials` once
   `/api/claude-auth/status` answers. A workspace boots unauthenticated -- the product's create path
   supplies no AI credentials -- so this keeps the graded agent in the same shared config-dir regime
   real workspaces run in. The paste mints a **provider account**, whose id the response carries.
   That is the default `anthropic` lane; a run on another
   [lane](#harness-and-model-arms) signs in through the workspace's accounts flow instead, which is
   the same user path for the harness that lane serves.
4. It then **creates the workspace's chat** through `/api/agents/create-chat`, named after the
   workspace host and bound to that account, and waits for it to reach WAITING. A workspace boots
   with no chat at all -- a chat binds to an account when it is created, and a fresh workspace has
   none -- which is why the sign-in has to come first: a create issued before it is refused for want
   of an account. A create whose answer is lost is retried, and the collision that retry hits is
   resolved back to the chat the first attempt left behind.
   Being the workspace's first chat, it is the one that gets `/welcome`. The greeting it draws is
   the trajectory's first agent message, before any client turn: the gates and the message-length check
   count only agent messages after the client's first turn, so it answers nothing there, while the
   judged transcript keeps it as the first agent message. Either way the driver waits for that
   welcome to be *answered* before turn 1. A
   new chat reports WAITING as soon as its agent is up, which is before the workspace has
   typed `/welcome` in; sending into that window would race the delivery and leave the greeting
   landing where turn 1's reply is read from. A run whose
   [harness config](#harness-and-model-arms) names a model switches the chat to it here, after the
   welcome has been answered and before turn 1.
5. It places the step's [uploads](#per-step-files), if it declares any, and then drives the case's
   turns. The loop has two levels: one pass per `prompts` entry, and within an entry one pass per
   exchange until its turn source says it is done or the loop stops it at the entry's budget. Each
   exchange starts by asking the entry's turn source what to do; a source that says it is done ends
   the entry there, without touching the workspace at all. For a message (literal, role-played by
   the decider model on `DECIDE_FROM_PERSONA`, or decided by the goal-holding client) the rest of
   the sequence follows: wait until the workspace chat agent is WAITING, send it, wait for the
   reply, snapshot the workspace if the cadence calls for it (the run recipe's `final` snapshots
   only after the last entry), and keep `/logs/agent/trajectory.json` + `state.json` current
   in the box. Turn sources never touch the environment: the loop owns all I/O, and a source only
   ever answers "say this" or "I am done". The welcome greeting is not part of that conversation, so
   a goal-holding client never sees it.
6. Once the last turn is done and while the workspace is still alive, the driver runs an
   **evidence-collection** phase: it records what was actually delivered (the app registry,
   supervisord's view of it, a file inventory, HTTP probes, declared test commands, UI flows, and
   the delivered repo as a git bundle) into `/logs/agent/verification/`, and captures the workspace
   agent's own common transcript, which then becomes `trajectory.json` (see
   [The trajectory](#the-trajectory)). It has to happen here, because the verifier runs after the
   workspace is destroyed. See [Outcome verification](#outcome-verification).
7. The **verifier** (pure rewardkit, separate container) scores the recorded transcript and
   evidence. See [Scoring](#scoring).

Steps 1-4 are the workspace bring-up, and they run once per trial. A case that declares
[steps](#stepped-cases) runs 5 and 6 once per step against the same, already-signed-in workspace,
with a verifier of its own after each; the workspace is torn down by the last step, or by the step
on which the trial gave up.

## Setup

- `~/.modal.toml` (run `modal token new` once) -- everything runs on Modal.
- `export ANTHROPIC_API_KEY=sk-ant-...` -- the decider (simulated user), the judge, and the
  credential the driver signs each workspace in with on the default `anthropic` lane. Set
  `ANTHROPIC_BASE_URL` alongside it to sign workspaces in against a proxy instead of the Anthropic
  API directly; under `--ak proxy=true` it is ignored, because the driver signs the workspace in
  against its own in-box proxy.
- A run on another provider lane also needs that lane's own key --
  `OPENROUTER_API_KEY` on the `openrouter` lane, `<PROVIDER>_API_KEY` on the `api-key` lane. See
  [Harness and model arms](#harness-and-model-arms). `ANTHROPIC_API_KEY` stays required whatever the
  arm, because the decider, the judges and the UI-flow agent call the Anthropic API on every trial;
  the lane's key is the *workspace's*, and is a separate concern from the harness's own spend.
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
- `just test-quick` / `just test-offload` skip this directory. This app's tests and type check run
  under `just test-minds-evals`, which the `test-minds-evals` CI job invokes on any PR touching this
  app or the monorepo packages it depends on.
- Type checking is split, because `imbue/minds_evals/resources/` and `imbue/minds_evals/templates/`
  are shipped as source into environments this project does not itself depend on. `resources/` runs
  in the box against the monorepo venv (importing `mngr_forward` and `playwright`) -- except the
  proxy hooks, which the proxy loads from the box's own `/opt/eval_proxy` venv, whose `litellm` is
  the version the monorepo venv is pinned to -- so this project excludes it and the root workspace
  checks it instead. `templates/` runs in the verifier container, whose only foreign import is
  `rewardkit` -- a dev dependency here purely so this project *can* check them, which it does; it
  is the root workspace that skips them. The repo-root `test_meta_ratchets.py` keeps the two configs
  from excluding the same path at once (it runs on every PR, unlike this project's path-gated job),
  and `rewardkit_pin_test.py` here keeps the dev-group `rewardkit` on the verifier container's own
  pin.
- Coverage omits both directories: neither runs in the dev process.

## Usage

```bash
# 1. Generate a dataset (one harbor task per persona case) from an eval config
just minds-evals-generate apps/minds_evals/configs/eval-config-small.json /tmp/minds-evals/datasets/small

# 1b. ...or pin a known-good pair without editing the config (branch, tag, or full SHA each)
uv run --project apps/minds_evals minds-evals generate \
  --config apps/minds_evals/configs/eval-config-small.json --output /tmp/minds-evals/datasets/pinned \
  --mngr-ref minds-v0.4.4 --dwt-ref minds-v0.4.4

# 2. Sanity-check the dataset end-to-end with the oracle (canned transcript; no Minds boot)
uv run --project apps/minds_evals harbor run -p /tmp/minds-evals/datasets/small -a oracle -e modal -y -o apps/minds_evals/jobs

# 3. Run the real eval (concurrency = simultaneous boxes; set it to the case count for one wave)
just minds-evals-run /tmp/minds-evals/datasets/small my-eval-run 3

# 4. Browse results
uv run --project apps/minds_evals harbor view apps/minds_evals/jobs

# Re-grade one finished rollout without re-running it (needs the task path)
uv run --project apps/minds_evals harbor trial regrade -p /tmp/minds-evals/datasets/small -e modal \
  -o apps/minds_evals/regrades apps/minds_evals/jobs/<job>/<trial>

# Or re-grade every trial of a finished job, into a new job beside the original
uv run --project apps/minds_evals harbor job regrade -p /tmp/minds-evals/datasets/small -e modal \
  -o apps/minds_evals/jobs apps/minds_evals/jobs/<job>
```

Both default to a local docker daemon, so `-e modal` is what makes them use the same environment the
run did. `-p` takes either one task directory or a dataset of them, in which case the task is matched
to the trial by `[task].name` and a name that matches none, or more than one, is an error. `-o` is a
*parent* directory in both, not the directory that gets written: `trial regrade` creates one
`<task>__<id>` trial directory under it and no job files at all, so only `job regrade` produces
something `harbor view apps/minds_evals/jobs` lists, and only it prints the mean-reward delta.

Neither touches the source: each seeds a new trial directory with the recorded agent logs and
artifacts and rebuilds the verifier from the task path given, which means it grades with **today's**
verifier and today's pinned rewardkit rather than the ones that recorded the trial. Gate criteria are
programmatic and reproduce exactly; judge criteria are sampled, so a regrade of an unchanged trial
still moves by whole likert points. Compare a regrade against another regrade, never against the
original run.

Generate datasets outside the repo tree: each generated task embeds a full mngr-internal clone, and
one under `apps/` trips the repo's marked-test discovery.

Each trial boots its own 6-CPU/16-GB box, so a full run is a **scheduled/nightly regression job,
not a per-PR gate**. Handy knobs:

- `-m/--model` selects the decider (simulated-user) model; default `claude-opus-4-8`.
- `--ak snapshot_mode=per-turn|final|off` controls workspace snapshot cadence; the run recipe
  passes `final`, and a later `--ak` wins. `per-turn` snapshots after every *exchange*, so a goal
  entry costs one tarball per exchange rather than one per configured entry, and `final` takes a
  single snapshot once the last entry is done. Extra harbor args are the recipe's *fourth* parameter,
  so `concurrency` must be given explicitly or they bind to it:
  `just minds-evals-run <dataset> <job> <concurrency> --ak snapshot_mode=per-turn`.
- `--ak verifier_model=<model>` runs the UI-flow verification agent on a different model from the
  decider (default: the decider's). Flow driving is mechanical, so a cheaper tier may do -- measure
  flow stability before changing the default.
- `--ak proxy=true` routes the workspace's model calls through an in-box LiteLLM proxy; see
  [Token and cost accounting](#token-and-cost-accounting).
- `--ak lane=`, `--ak key_provider=`, `--ak key_env=`, `--ak model=`, `--ak effort=` and
  `--ak fast=` pick the run's **harness config**: the harness, model, effort and speed tier every
  case of the run drives on, which with the (mngr, dwt) pair makes up the run's arm. See
  [Harness and model arms](#harness-and-model-arms).
- `-k/--n-attempts N` runs each case N times (judge scores are statistical; use means).

Results land in `apps/minds_evals/jobs/<job>/<trial>/`: harbor's `result.json` and
`verifier/reward-details.json` at the trial root, and everything the driver collects under `agent/`
-- `trajectory.json`, `state.json`, `snapshots/`, `usage.json`, `driver.log`,
`driver_events.jsonl`, `instruction.md`, and `verification/` (plus `timeout_diagnostics.json` when
the trial gave up). They stay there; the
recipe uploads nothing. Archiving belongs to whatever runs the eval on a schedule, which supplies
its own credentials rather than reading a developer's.

## Harness and model arms

An **arm** is the whole treatment a trial ran under: the (mngr, dwt) pair its box and workspace were
built from, together with the **harness config** -- the harness, model, effort and speed tier the
chat drives on. Both halves are run-level, never per-case: every case of a dataset runs on the same
pair and the same harness config. The pair is fixed when the dataset is generated, and the kwargs
below pick the harness config; the dataset is untouched by them, so every harness config run
against a dataset shares its one image build and one generation.

The harness follows the **provider lane** the workspace is signed in on, because that is how the
product itself decides it; the model, effort and speed tier are then set through the product's own
model endpoint before turn 1. The lanes that can be signed in without a human are `anthropic` (the
claude harness) and `api-key`, `openrouter` and `opencode-go` (all pi-coding). The `openai` lane
(codex) needs a device-auth flow on a PTY, so it is not available here.

| kwarg | meaning | default |
|---|---|---|
| `--ak lane=<id>` | the provider lane to sign the workspace in on | `anthropic` |
| `--ak key_provider=<id>` | which provider the key belongs to (`anthropic`, `openai`, `openrouter`, ...) | required on the `api-key` lane, rejected on every other lane |
| `--ak key_env=<VAR>` | the environment variable holding the lane's key | derived from the lane; see [The lane's key](#the-lanes-key) |
| `--ak model=<id>` | the catalog id to switch the chat to before turn 1 | unset: no switch |
| `--ak effort=<level>` | the effort or thinking level to set with the model | required with `model` |
| `--ak fast=<bool>` | the speed tier to set with the model | `false` when `model` is given |

`model` and `effort` come together, and `fast` needs a `model` to hang off: the model endpoint
refuses one without the other and needs a model id before it will apply any axis, and no HTTP
endpoint reads the live choice back for the driver to fill one in from. So `model` without `effort`,
`effort` without `model`, `fast` without `model`, a `key_provider` off the `api-key` lane, the
`api-key` lane without one, a lane that is not in the table, and a lane whose key variable is
neither derived nor named are all refused when the driver is constructed -- before a box boots,
rather than five minutes into a run. `--ak proxy=true` is refused off the `anthropic` lane too,
because the in-box proxy can only meter that one; see
[Token and cost accounting](#token-and-cost-accounting).

**The default harness config** is a lane and its credentials and nothing else: no `model`, no
`effort`, no `fast`. It makes no switch and changes no setting of the workspace, so the chat runs
exactly as the product ships it -- on claude the pinned model in fast mode, on pi-coding the
provider's default at standard speed (`claude-opus-4-8` on an Anthropic key). It still records
everything below; it simply requests nothing. A run with no harness flags at all is the default
harness config on the `anthropic` lane, on whichever pair the dataset was generated from.

**Every other harness config names `model` and `effort` and leaves `fast` at `false`.** Fast mode
bills the same tokens at twice the rate and changes nothing else, so configs compared on cost must
all run standard. The default harness config is the one exception, and its cost reads as the
product's own cost rather than as a point on that comparison.

Model ids are the workspace's **catalog ids**, not API model names, and they differ by harness
(measured 2026-09-08). claude offers `fable[1m]`, `opus[1m]`, `sonnet[1m]` and `haiku`, with efforts
`low`, `medium`, `high`, `xhigh` and `max`. pi-coding offers `<provider>/<model>` tags gated by the
account's key -- `anthropic/claude-haiku-4-5`, `openrouter/<vendor>/<model>` -- with thinking levels
`off` through `max` per model. The driver validates none of these strings itself: the endpoint's own
400 is the validation, and the trial's reason quotes its detail (to 300 characters), so a typo in
a catalog id is legible from the trial listing.

```bash
DS=/tmp/minds-evals/datasets/small

# the default harness config on the anthropic lane
just minds-evals-run $DS default 3
# opus at standard speed, for cost comparisons against the other arms
just minds-evals-run $DS opus-standard 3 --ak model='opus[1m]' --ak effort=high
# a cheap claude config
just minds-evals-run $DS haiku 3 --ak model=haiku --ak effort=medium
# pi-coding on an Anthropic key
<<<<<<< HEAD
just minds-evals-run $DS pi-anthropic 3 --ak lane=api-key --ak key_provider=anthropic \
  --ak model=anthropic/claude-haiku-4-5 --ak effort=medium
# pi-coding on OpenRouter
OPENROUTER_API_KEY=... just minds-evals-run $DS pi-openrouter 3 --ak lane=openrouter \
  --ak model='openrouter/<vendor>/<model>' --ak effort=medium
=======
just minds-evals-run $DS pi-haiku 3 --ak lane=api-key --ak key_provider=anthropic \
  --ak model=anthropic/claude-haiku-4-5 --ak effort=medium
# pi-coding on OpenRouter
OPENROUTER_API_KEY=... just minds-evals-run $DS pi-gpt-5-mini 3 --ak lane=openrouter \
  --ak model=openrouter/openai/gpt-5-mini --ak effort=medium
>>>>>>> origin/main
```

One dataset serves every harness config, and each run needs its own job name, because harbor refuses
to reuse one.

<<<<<<< HEAD
=======
**The named harness configs** live in `configs/harness_configs.json`: `default`, `haiku`,
`pi-haiku`, `pi-gpt-5-mini`, `pi-glm-4.7-flash` and `opus-standard`, each a name, an `is_nightly`
flag and the kwargs above. It is the list the [scheduled CI](#scheduled-ci) composes its cells from,
and the place to look for a config that is known to work -- an entry's kwargs are exactly the flags
to append to a `just minds-evals-run` line to drive the same arm locally, and the entry's own name
is a job name that says which arm the run was.

>>>>>>> origin/main
### The lane's key

The driver reads the workspace's key from the variable `key_env` names. Left unset, it derives one:
`ANTHROPIC_API_KEY` on the `anthropic` lane, `OPENROUTER_API_KEY` on the `openrouter` lane, and
`<KEY_PROVIDER>_API_KEY` on the `api-key` lane, upper-cased with dashes turned into underscores
(`OPENAI_API_KEY`, `OPENROUTER_API_KEY`). The derivation is a convenience, not a contract with the
workspace template: the template names the variable per provider and does not always follow the
pattern (`google` reads `GEMINI_API_KEY`), and `opencode-go` has no derived default at all. Name the
variable with `key_env` in those two cases. A variable that is unset stops the run at construction,
naming the variable and the lane; a lane outside the table above is refused there too, and no
`key_env` makes one runnable.

`just minds-evals-run` still requires only `ANTHROPIC_API_KEY` and does not learn about the other
variables; the decider and the judges need it on every arm, and the driver's own check is what
covers the lane's key.

### The switch, and the greeting before it

The workspace's first chat is greeted by `/welcome`, delivered by the create template the moment the
agent is ready. Nothing a client sends can decline it, and a switch typed into that window would
race the delivery, so the harness config is applied only once the greeting has been answered. The
**greeting therefore always runs on the workspace's default model in the template's speed tier**,
whatever the run asks for, and the requested model serves every turn after it. On a trivial case the
greeting is most of the spend.

In `usage.json` the greeting is already its own `per_model` row whenever it ran on a different model
from the turns, and `arm.harness_config.welcome_model` names that row; a reader comparing arms
subtracts it. Nothing else about `usage.json`'s shape changes.

A [stepped case](#stepped-cases) prepares its workspace once, on its first step, so the harness
config is applied once and every step's `state.json` names the same arm. The observed half is each
step's own: it is read off the transcript that step captured, so a step that captured none records
no models while an earlier one recorded some.

### What the arm records

`state.json` gains an `arm` block, and the trajectory's `extra.minds_evals` and the trial metadata
carry the same one, so the driver's record and the graded document both say what the trial was asked
to run and what it was observed running:

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

- `mngr_sha` and `dwt_sha` are the SHAs the box was built from and the workspace was cloned from --
  the same pair `state.json` and the trial metadata carry at their top level, repeated here so one
  arm block names the whole treatment without a reader having to assemble it. Everything under
  `harness_config` is the other half: what the run asked the chat to be, and what it was seen being.
- `harness` is read back from the workspace's own accounts listing after sign-in rather than assumed
  from the lane. It is the driver's only reading of the harness, and it comes from the template.
  Grading takes a second one off the trajectory (see [Scoring](#scoring)), and the two can disagree.
  It stays empty where there was nothing to read: a sign-in that settled without naming the account
  it minted, or a listing with no row for that account.
- `model_choice_switch` is `applied`, `skipped` (the run named no `model`), or the failure that
  stopped the trial. It is empty on a trial that named a model and gave up before the switch could
  be applied. The switch is a single call setting model, effort and fast at once, which is the
  product's own model choice (`POST /api/agents/<id>/model`).
- `observed_models` are the model names on the transcript's agent steps after the client's first
  turn, and `welcome_model` the one on the greeting step. The greeting and the conversation are
  separated at the step carrying the driver's own first message -- the first `user` step whose
  stripped message is that text -- so the greeting falls on the greeting side whether the harness
  filed it as a `user` step (pi) or a `system` step (claude), and neither harness's greeting text is
  spelled out anywhere. A step filed under the `<synthetic>`
  pseudo-model does not count towards either: that is a message the harness wrote itself, which no
  inference answered, and counting it would read a switched trial as one that ran on two models.
  Both are read straight out of the captured
  document, so they are available on every harness -- a document that was captured but did not
  validate fills them in even though [the trajectory](#the-trajectory) beside them is the hand-built
  one. They go empty together, and a trial that captured no document is not the only one that leaves
  them so: a captured document carrying no step that matches the driver's first message (its shape
  changed, or the trial never sent a turn) records no models either, logs a warning, and leaves
  `is_model_confirmed` `null` as any other silence does.
- `is_model_confirmed` is `true` when exactly one model was observed and it is the requested one,
  under the harness's own naming (`haiku` reports as `claude-haiku-4-5-20251001`, and
  `anthropic/claude-haiku-4-5` as `claude-haiku-4-5`). A config that named no model has nothing to
  confirm, so every default-config trial records `null`. A config whose catalog id the naming table
  cannot translate into a reported name (only a claude-shaped id can be untranslatable, since a
  `<provider>/<model>` tag reports as itself minus its first segment), and a trial that observed no
  model at all (its transcript was never captured, or it gave up before a turn), leave it `null` as
  well, never `false`: silence is not evidence of a wrong model. On
  a proxied `anthropic` trial the proxy's `per_model` is the ground truth and the confirmation is
  computed from that instead -- the requested model has to be there, and every other row has to be
  the greeting's, since the proxy cannot tell which turn served which request.

A trial that silently ran on the wrong model is the failure worth catching, so read
`is_model_confirmed` before reading a comparison. [`check-run`](#checking-a-finished-run) fails a
trial whose `false` says it answered on another model, so a switch that did not take breaks the run
loudly instead of skewing a comparison quietly.

### Comparability

An arm changes the system under test, in either half. A run with `fast=false` is not comparable to
a run that left the template's fast mode on -- which is what the default harness config does -- and
a pi-coding run is not comparable to a claude run on any dimension but cost. Two arms that differ in
both their pair and their harness config attribute nothing to either, so vary one at a time.
Grading differs too: `harness_quality` is not scored on a harness other than claude, and the reward
drops its share (see [Reward composition](#reward-composition)). Version or flag result sets at the
arm, the same way they are versioned at the adoption of a goal entry.

## Browsing results

```
just minds-evals-view                                  # apps/minds_evals/jobs on :8080
just minds-evals-view /path/to/other/jobs 8090
```

The backend is stock harbor; only the frontend is ours. `apps/minds_evals/viewer/` is a verbatim
copy of harbor's own `apps/viewer`, vendored so that eval-specific annotations can be rendered
without forking harbor's Python -- `harbor.viewer.create_app` accepts the static directory as an
argument, which is the whole seam. See `viewer/VENDORED_FROM.md` for the tag it came from and how
to re-vendor.

The recipe builds first when the sources are newer than the last build, bootstrapping a pinned bun
into `apps/minds_evals/.bun` on first use: about ten seconds once, two after, and nothing on a
no-op. Neither the toolchain nor the build output is committed.

Two properties are worth knowing. The vendored tag must match the harbor pin in `pyproject.toml`,
because a newer frontend calls endpoints an older backend does not serve; `viewer_contract_test.py`
checks every URL in the vendored client against the routes harbor actually registers, since nothing
else connects the hand-written TypeScript to the Python. And a job directory stays fully readable by
stock `harbor view`, which renders none of our annotations but everything else -- that fallback holds
as long as annotations live in ATIF `extra`, which upstream ignores.

## Diagnosing a trial that went wrong

Five artifacts answer "what happened", in the order worth reading:

- `agent/state.json` -- `test_state`, and, when it is `timed_out`, `timed_out_reason`: prose naming
  what the trial gave up on. Preparation names its own wait (an auth endpoint that never came up,
  credentials the workspace refused, a chat that was never created or never reached WAITING or
  never answered its welcome, the in-box proxy not coming up); the
  conversation names the message it stopped at (`could not send message N`, `no reply to message
  N`, `agent never reached WAITING before message N`); a stepped case adds the step's own uploads
  (`could not create the workspace's uploads directory ...`, `could not place the step's upload
  ...`) and the workspace an earlier step took with it (`an earlier step failed and tore the
  workspace down, so this step has none to drive`, which is what a later step of an aborted trial
  reports). A run's [harness config](#harness-and-model-arms) adds reasons of its own, listed below.
  The same string is on the trial metadata.
- `agent/driver.log` -- this driver's own timestamped log for the run, written per step (for a
  stepped case these, and everything else under `agent/`, live under `steps/<name>/agent/`). Without
  it loguru goes only to the harbor process's stderr, which no artifact keeps. Every readiness poll
  reports once a minute that it is still waiting and what the workspace is answering meanwhile (the
  agents listing, the chat agent's state, or that the bridge is answering nothing at all), so a wait
  that never finishes says why.
- `agent/driver_events.jsonl` -- what the harness saw, as distinct from what the workspace
  recorded: the workspace UI feed the driver polled, followed by one record per decider-model call
  with the message it produced (the trajectory's `extra.minds_evals.decider_turns` carries the same
  calls without their text). Written host-side after every turn and again once the evidence phase is
  done, never mirrored into the box, and touched by no grade-time reader. Reach for it when the
  conversation went wrong on the harness's side of the wire: replies the driver could not make out, a
  decider that answered with something other than the message that reached the workspace, or an eval
  that has drifted from the workspace template it drives.
- `agent/timeout_diagnostics.json` -- written only when the trial gave up, and only then: the
  workspace's `/api/agents` body, the chat agent's state, and the tails of the three box service
  logs, captured while the workspace still existed. Every capture is guarded and the whole bundle
  is bounded, so a capture that fails records its failure text rather than losing the rest.
- `artifacts/logs/artifacts/minds/` (for a stepped case,
  `steps/<name>/artifacts/logs/artifacts/minds/`) -- the box's own service logs, in full: `box.log`
  (the Minds backend), `reverse_tunnel.log`, and `proxy.log`. These live outside `/logs/agent`
  deliberately: harbor empties that directory before every step of a multi-step task, which would
  unlink `box.log` while the backend kept writing to the dead inode. Snapshots stay under
  `agent/snapshots/` instead, because a finished tarball has no writer holding it open and the
  service logs dir is re-collected in full on every step.

`agent/instruction.md` sits beside these: the instruction harbor handed the driver, kept where a
reader meets it next to the trajectory it drove. `harbor view` browses a trial's files under
`agent/`, `artifacts/` and `verifier/` only, so a copy anywhere else in the trial would be listed by
the API and shown by no tab. A stepped case gets each step's own instruction, since harbor gives each
step its own agent directory; a case without steps gets the whole case's. It is written before the
instruction is parsed, so one that cannot be parsed is still on disk to look at, and it is never
mirrored into the box -- the expectations it carries have no business on the machine the agent under
test runs on.

Workspace preparation -- create, sign-in, creating the chat, waiting out its welcome, applying the
[harness config](#harness-and-model-arms) -- runs against its own 1200s budget rather than the case's
`timeout_seconds`, so a workspace that comes up dead is
reported as such within twenty minutes instead of consuming the whole case. The conversation deadline
still caps it (whichever is sooner wins) and still governs the turns themselves. A reason that names
that ceiling is one of those preparation waits; only they quote it. A reason that does not may still
be a wait -- the in-box proxy, an uploads directory, an upload placement -- and says which operation
ran out instead, or it may be a failure the driver could tell immediately, such as the workspace
refusing the credentials it was given. A run with no key at all never reaches a trial: that is
refused when the driver is constructed, so it leaves no `state.json` to read a reason off.

**The harness config's own failures** name it, in the same style, so they read like any other
reason. The
sign-in on a lane other than `anthropic` contributes `the workspace refused to start a sign-in on
lane <lane>: <detail>` (the sign-in flow would not start) and `the workspace rejected the key for
lane <lane>: <detail>` (the workspace would not take the key against the flow -- usually because it
probed the harness with it and it did not work there, but a flow already spent answers here too),
and `the sign-in on lane <lane> never settled` for a flow still pending at the deadline. Every lane
waits for the chat app to answer before any of that, and that wait is the one the claude sign-in
uses, so `the workspace's claude-auth endpoint never came up` is a chat app that never came up and
is reported on every lane, not just `anthropic`. The model
switch contributes `the workspace refused the requested model choice: <detail>` (the endpoint answered
400 -- a configuration error, never a workspace fault, and the detail is what makes a mistyped
catalog id legible), `the workspace could not apply the requested model choice: <detail>` (every other
answer that is not a 2xx, a 500 and no answer at all included), and `the workspace chat never
settled after the model switch`. The refusals the
driver can tell at construction stop the whole run before any box boots, and the one for a missing
key names it: `no <VAR> to sign the workspace in on lane <lane>`.

### The trajectory

`trajectory.json` is the trial's only conversation record and the one every grade-time reader takes
the conversation from, exactly as for any other harbor eval: the judge-transcript renderer, the
structural gates, and the message-length guard read its ATIF steps, and the judges read the rendering.
Nothing at grade time knows the workspace UI feed exists.

Two transcripts of the workspace agent exist: the workspace UI feed (`/api/agents/<id>/events`),
which the driver polls to detect each reply and price its usage, and mngr's own **common
transcript** (the ATIF-shaped `header`/`step`/`observation` stream at full fidelity, see
`specs/atif-transcript-alignment/spec.md`). The trajectory comes from the latter whenever the
workspace can provide it:

- While the trial runs, the driver keeps `trajectory.json` current in the box after every turn as
  its own hand-built summary of the clean conversation (one step per client turn and per merged
  agent reply), so a trial that dies mid-way still leaves a gradeable record.
- Once the evidence phase has captured it, the driver replaces that with the ATIF document
  `mngr transcript --format atif` built inside the workspace (tool calls, observations, thinking,
  embedded proxy-subagent trajectories), with `final_metrics` replaced by the trial's resolved usage
  and an `extra.minds_evals` block naming the driver, the decider model and its turns, the case, the
  usage source, and the [arm](#what-the-arm-records) the trial ran on.
- Background workers the agent launched through the launch-task skill (`create_worker.py launch
  --name <x>`, a separate mngr agent in the same workspace) are discovered from the launch commands
  in its own stream, captured one by one (`mngr transcript` for a worker still in place, mngr's
  preserved copy of the stream for one destroyed after finishing), and embedded in `trajectory.json`
  under the launching call as ATIF `subagent_trajectories` with `subagent_kind: "mngr"` and an
  `extra.worker` block. Launches are followed three levels deep: the chat agent's workers, their
  workers, and theirs. The report each worker pushed back to its lead is captured beside it.
- `metadata.trajectory_source` (`workspace`, `hand_built`, or `none`) and
  `metadata.transcript_capture` say which shape the file has and, when the capture failed, why;
  `metadata.workers` lists each launched worker with what was captured for it and its own usage.
  `none` means no `trajectory.json` was written at all: the trial never exchanged a message, so
  there was no conversation to hand-build, and no captured document reached the box either.

A multi-step task drives one workspace across several instructions, and every step's trajectory
replays the conversation from its first turn, so the driver marks each step's first turn with a
`system` step naming it (`Step: <name>`, tagged `extra.minds_evals.kind: "step_boundary"`), under a
`MINDS EVALS` banner rule that sets it apart from the long `system` steps the workspace's own
transcript contributes. The marker is cosmetic: `system` is the source every grade-time reader already skips, so no judge, gate,
or word count sees it, and `final_metrics.total_steps` stays the conversation's own count. In the
workspace's own document the marker is placed at the step's opening client message, or by timestamp
when that message is not in the document; a boundary that resolves to neither is dropped rather than
guessed at. A task without steps has nothing to divide and gets no marker.

A workspace whose mngr predates ATIF cannot answer `mngr transcript --format atif`, and any other
capture failure (bridge, pull, download) is recorded the same way: grading proceeds on the hand-built
document, which carries the same `extra.minds_evals` block with `source: "hand_built"`. Two problems
arise after a successful capture and fall back the same way -- a captured document that is not valid
ATIF, and a final upload of `trajectory.json` that cannot reach the box; they leave the document half
marked captured beside `trajectory_source: hand_built` (the last per-turn copy stands in the box), and
their cause is in the driver's log rather than the metadata. A failed final upload on a trial with no
exchange has no per-turn copy to fall back on, so it reports `none`. The capture never adds a manifest
entry, so a transcript problem can never read to the outcome judge as an unmeasured deliverable
check. The bundle keeps the captured stream (`verification/common_transcript.jsonl`) and the
unmodified document (`verification/workspace_trajectory.json`) as evidence. Design and
consumer-by-consumer notes: `specs/minds-evals-atif-transcripts/spec.md`; the worker capture is in
`specs/minds-evals-worker-trajectories/spec.md`.

A [stepped case](#stepped-cases) captures and publishes once per step, and each step's
`trajectory.json` describes the **whole conversation so far** rather than that step alone: the steps
share one workspace, so the document its agent builds is cumulative, and the hand-built shape is
built from the same accumulating conversation. A worker still alive when a later step collects is
captured again by that step, so every step's bundle and trajectory stand on their own.

Because each step's trajectory replays the conversation from its first turn, the driver marks each
step's first turn with a `system` step naming it (`Step: <name>`, tagged
`extra.minds_evals.kind: "step_boundary"`) under a `MINDS EVALS` banner rule, so the step being
graded is legible against the ones before it. The marker is cosmetic: `system` is the source every grade-time reader already skips, so
no judge, gate, or word count sees it, and `final_metrics.total_steps` stays the conversation's own
count. In the workspace's own document the marker is placed at the step's opening client message, or
by timestamp when that message is not in the document; a boundary that resolves to neither is dropped
rather than placed on a guess. A case without steps has nothing to divide and gets no marker.

## Eval config

The configs this README walks through live in `configs/`: `eval-config.json` (nine cases),
`eval-config-small.json` (three, two of them carrying `expectations`) for quick end-to-end runs,
`eval-config-stepped.json` (one three-step [stepped case](#stepped-cases), whose uploads live in
`configs/datasets/`), and `eval-config-probe.json` (one case, one literal turn, no expectations),
which asks the agent to name the model it is running as and is the cheapest way to check that a
[lane or a harness config](#harness-and-model-arms) works at all. `configs/` also holds
`eval-config-project-roadmap.json` (the same roadmap persona in two steps) and the two
`eval-config-todo-app-*` A/B pairs (`-scripted` and `-harden`, each beside its `-control`), whose
halves differ only in the `dwt_branch` they pin, so a workspace-template change is measured against
a run without it. All pin `mngr_branch: main`. A config naming
a branch that no longer exists fails at generation time, when the branch is resolved to a SHA -- so
a config pinned to a feature branch is worth keeping only while that branch is. A ref given as a full SHA is taken at
its word and costs no remote lookup, so a SHA that was never pushed is caught later: for mngr by
the shallow clone at generation time, and for the workspace template not until the box clones it
inside every trial.

```json
{
  "mngr_branch": "main",
  "timeout_seconds": 3600,
  "personas": [
    {"id": "todo-app", "persona": "...", "prompts": ["Build me ...", "Sounds good.", "DECIDE_FROM_PERSONA"]}
  ]
}
```

- `mngr_branch` is resolved to an exact SHA at generation time and recorded in each task's
  `[metadata]`; the box is built from that SHA. Despite the name it takes any ref: a branch, a tag
  (annotated tags are peeled to their commit), or a full 40-hex SHA, which is used as-is. A name
  carried by both a tag and a branch resolves to the tag. `--mngr-ref` on the `generate` command
  overrides it for one generation, which is how a scheduled run pins a known-good release pair
  without editing the checked-in config; `--dwt-ref` does the same for `dwt_branch`. The metadata
  records whichever ref was actually used.
- `dwt_branch` (on `dwt_repo`, the workspace template; defaults to `main` on
  `imbue-ai/default-workspace-template`) is pinned the same way and accepts the same refs:
  generation resolves it to an exact SHA, records it as `dwt_sha` in `[metadata]` next to the ref it
  came from, and the box clones
  that SHA. So a dataset builds the same workspaces however long after generation it is run --
  **picking up new template changes requires regenerating the dataset**. Each trial's own record
  carries `mngr_sha` and `dwt_sha` too (in `state.json` and the agent metadata), so a captured trial
  says which mngr and which template produced it.
- A string `prompts` entry is one turn: a literal message sent verbatim, or `DECIDE_FROM_PERSONA`
  (the decider role-plays the client from the persona plus the transcript so far; cannot be the
  first entry).
- An entry may instead be a **goal object**, `{"goal": "...", "max_exchanges": 3}`, which expands
  into a bounded back-and-forth: a goal-holding client keeps replying until it declares itself
  satisfied or the budget runs out. One model call per exchange decides both questions at once
  (say the next thing, or stop). `max_exchanges` defaults to 3 and is capped at 8, because each
  exchange is a full agent turn in a real workspace; generation warns when a case's worst case
  cannot fit its `timeout_seconds`. The first entry must stay a literal string, so a case's opening
  ask is deterministic. The client judges satisfaction **from the conversation alone** -- it never
  reaches into the workspace, and the evidence phase plus outcome judge remain the ground truth for
  whether the goal was actually achieved.
  **Scores are not comparable across the adoption of a goal entry**: a persistent client changes the
  conversation being measured, so version or flag result sets at that cut point.
- `state.json` also carries `timed_out_reason`: empty while the trial is going, and otherwise prose
  naming which wait ran out. `timed_out: true` on its own cannot tell a workspace that never came up
  from an agent that stopped replying halfway through.
- `elapsed_seconds` is the whole trial's, and `step_elapsed_seconds` is this step's -- the span
  `timeout_seconds` bounds, since for a stepped case that key is only the step's share of the
  conversation budget. On a flat case the two agree.
- Each entry's outcome is recorded in `state.json` under `entries`, as
  `{index, kind, exchange_count, outcome, detail}` with `outcome` one of `completed`, `satisfied`,
  `budget_exhausted`, or `fallback`, and `detail` why the entry stopped: for `satisfied` the
  client's own satisfaction reason, which is always present because a satisfaction with no reason is
  treated as no answer at all; for a `fallback` the harness's note that the client's model call
  failed, which every `fallback` carries whether the client reported it or the budget stopped the
  entry first. It is empty otherwise. `waits_done` counts the messages actually sent, which a goal
  entry can push past `num_turns` (the configured entry count). A `budget_exhausted` entry does not
  zero the reward -- an agent that cannot satisfy an unreasonable goal is not a broken trial -- and
  the exchanges it produced stay in the conversation the judges grade. The outcome labels themselves
  are read only by the structural gate, not by the judges, which grade the rendered conversation.
  An entry only earns a record once it has stopped, so a timed-out trial's `entries` ends at the
  entry it died in: that entry and any after it are absent, and `waits_done` can then exceed the
  exchanges the records account for.
- `verification_timeout_seconds` (default 1800) is the evidence-collection phase's own budget. It is
  *added* to the task's `[agent].timeout_sec` (case timeout + verification budget + grace), so
  verification never competes with the conversation for time. It is a deadline, not a reservation:
  a case with no UI flows finishes the phase in a couple of minutes and the rest is never spent.
- Each persona entry may carry an `expectations` block; see below.
- A case may declare `steps` **instead of** `prompts`; see below. Declaring both is rejected, as
  is a case-level `expectations` on a stepped case.

## Stepped cases

A case that declares `steps` becomes a harbor multi-step task: the driver is invoked once per step
against one workspace, every step is verified by the standard verifier with that step's own
expectations, and a step's `min_reward` decides whether the trial may go on.

```json
{
  "id": "project-roadmap",
  "persona": "Head of product at a small startup. Non-technical, but knows their own projects well.",
  "reward_strategy": "final",
  "steps": [
    {
      "name": "build-from-data",
      "files": [{"source": "datasets/roadmap-v1", "upload_id": "41e940fcd33540078ab77fd79f3b3943"}],
      "prompts": [
        "Can you build me an editable roadmap tool? The data is in /home/user/workspace/data/uploads/41e940fcd33540078ab77fd79f3b3943. Sketch me something first.",
        {"goal": "See a concrete mockup and sign off on it", "max_exchanges": 4}
      ],
      "expectations": {"outcome": "The agent presented a concrete mockup and the client approved it."},
      "min_reward": {"gates": 1.0, "outcome": 0.5}
    },
    {
      "name": "updated-dataset",
      "files": [{"source": "datasets/roadmap-v2", "upload_id": "985e2d4f7eb948b3b45a8f0923521ab8"}],
      "prompts": ["Here is an updated pull, in /home/user/workspace/data/uploads/985e2d4f7eb948b3b45a8f0923521ab8."],
      "expectations": {
        "outcome": "The running roadmap reflects the updated export.",
        "deliverable": {"kind": "minds-app"},
        "ui_flows": [{"name": "updated-content", "steps": "Open the roadmap.", "expect": "The new milestones are shown."}]
      }
    }
  ]
}
```

The block above is abridged to two steps. `configs/eval-config-stepped.json`, with the datasets in
`configs/datasets/`, is the full worked example: the same client, with a middle
`adjust-requirements` step between the two shown here.

- One **workspace** for the whole trial, prepared on the first step and torn down after the last,
  or on a step the driver itself gave up on. A step that merely scored below its `min_reward` is
  not one of those: harbor decides that after `run()` has returned, so nothing in the driver sees
  it, and the workspace sandboxes -- which outlive the box that made them -- are reclaimed by their
  own 3h lifetime instead. A gate-aborted trial therefore leaves them idling until then. The
  Minds conversation lives in that workspace, so the client and the agent simply carry on across a
  step boundary -- nothing is replayed or resumed.
- A step's `prompts` is exactly a flat case's `prompts`, goal entries included. Only the case's
  *opening* ask (the first entry of the first step) must be a literal string; a later step opens
  mid-conversation, where there is a transcript for the client to decide from.
- A step's `expectations` has exactly the case-level schema, and a step that omits it is graded on
  the structural gates and the conversation alone. A **case-level** `expectations` on a stepped case
  is rejected: every step states its own, so that a reader of a step sees what that step is graded
  on. A step whose expectations carry no `deliverable` and no `ui_flows` commissions nothing
  probeable and is judged from the conversation -- which is what an early phase ("a mockup was
  presented and approved") wants.
- `min_reward` is the reward the step must reach for the trial to continue, in harbor's own form:
  a bare number gates the composed `reward` key, and an object gates each dimension it names
  (`gates`, `quality`, `harness_quality`, `outcome`, `reward`). A dimension the object leaves out is
  not gated; a dimension it names but the verifier did not produce counts as `-inf` and always
  fails. That is why a `harness_quality` floor belongs only in a dataset run on the claude
  [harness](#harness-and-model-arms): the dimension is not scored on any other harness, and
  generation cannot refuse the floor because the dataset does not know which harness config will
  run it. Below the
  threshold, harbor **aborts every remaining step** -- there is no continue-past-failure.
  The recommended shape is `{"gates": 1.0, "outcome": <threshold>}`: the structural gates are binary
  and the outcome score is graded, so the threshold is a judgment the author calibrates from the
  `reward-details.json` of a first run.
- The **last step may not declare a `min_reward`**, and generation rejects one that does: harbor's
  threshold only ever aborts *later* steps, so one there would be graded and then ignored.
- A non-final step **without** `min_reward` has no abort path: after an earlier failure harbor still
  runs the next step, against a workspace that has already given up. Generation warns, and the
  driver fails that step fast rather than spending its budget rediscovering the same dead workspace.
- `reward_strategy` selects harbor's `multi_step_reward_strategy`: `final` (the default) scores the
  trial by the last step that ran, and `mean` averages every step that produced a reward. Both are
  legitimate because every step is graded by the same verifier on the same scale as a flat case.
  Under `final` a gate-aborted trial is scored by the aborted step's own graded reward -- a real
  measurement of the step the agent failed. Under `mean`, note that aborted steps produce no reward
  at all rather than a zero, so an early abort *raises* the mean; the aborted and completed trials
  are different populations either way and must not be pooled.
- A trial whose step verifier could not produce a reward at all (a judge failure, an unparseable
  reward file) stops there too -- harbor aborts the remaining steps on a step that has an exception
  and no verifier result -- but the trial is recorded as an **error** rather than as a scored
  failure. That is the same distinction a flat case makes between "the agent fell short" and "the
  harness could not find out", and it is why such trials must be excluded rather than read as zeros.
- Generation also rejects, beyond the rules above: a step `name` that does not match
  `^[a-z0-9][a-z0-9-]*$` (it has to serve as a task subdirectory, a harbor step and a verifier
  container session at once), a name repeated within the case, an unknown key in a step object or in
  a `files` entry, a `source` that is absolute or climbs out of the config's directory with `..`, an
  `upload_id` that does not match `^[A-Za-z0-9][A-Za-z0-9._-]*$` (it names a directory in the
  workspace and in the box, and is quoted into prompts as a path), a `files` value that is not a
  list, a `reward_strategy` on a case with no `steps`, a `min_reward` that is neither a number nor an
  object, one whose key is not a reward dimension or whose floor is not a number, an empty
  `min_reward` object (which would gate nothing), and an `outcome` floor on a step that declares no
  `expectations` -- that step's verifier emits no outcome score, so harbor would read the missing
  key as `-inf` and abort the trial there on every run.

### Per-step files

A step's `files` are what the client "uploaded" for that phase. They do not exist in the workspace
before that step, and that is a fact of the filesystem rather than a convention: the file is not in
the template, not in the box image, and not in the workspace until the driver places it. Shipping
every dataset from the start and pointing at each by an opaque directory name only hides the future
from an agent that does not look.

- `source` is a file or directory **relative to the eval config file**, and `upload_id` is the
  directory it appears under in the workspace's `data/uploads/`, so a prompt can quote the same path
  the client would see in Minds. Each `upload_id` must be unique across the case; a missing source
  or a duplicate id fails generation.
- Files travel in two hops, because neither end can reach the other directly. Generation copies each
  source into `steps/<name>/workdir/step_files/<upload_id>/`; harbor merges that `workdir/` into the
  box's working directory before the step's agent runs and executes the generated `setup.sh`, which
  relocates the uploads to `/work/step_files/<name>/` and deletes itself -- the box's working
  directory is the mngr checkout every workspace is vendored from, and must stay what the image
  shipped. The driver then makes the workspace's `data/uploads/` and copies each upload in with the
  same `mngr rsync` the snapshot pull uses in the other direction, before the step's first message.
  That transfer creates its own destination tree; the explicit directory call ahead of it is what
  lets a workspace that will not take the directory at all be reported as that rather than as a
  broken upload.
- They land **untracked** (the template ignores `data/uploads/*`), exactly as a real upload does, so
  they never enter the eval-case commit or the captured deliverable. Whether the agent actually used
  them is the outcome judge's and the UI flows' question, not a file-inventory check.
- A placement that fails marks the trial timed out with that reason: a conversation about an upload
  that is not there measures nothing.
- Keep the datasets small. A source is copied into every task directory that uses it and travels
  into the box once per step.

### Per-step verification

The evidence phase runs at the end of **every** step, against that step's expectations and within
its own `verification_timeout_seconds`, while the workspace and the app inside it are still alive.
A step that commissions no deliverable collects the always-on capture plus any UI flows and
`test_commands` it declares -- no HTTP or file probes, and no deliverable bundle. Only the bundle is
tied to `deliverable`; `ui_flows` and `test_commands` are declared independently of it, so a step
can probe or exercise what an earlier step delivered without commissioning anything of its own. The
workspace is torn down after the last step, or on the step where the trial gave up.

That is the expensive part of a trial (browser flows, screenshots, judge calls, a bundle, a
snapshot), so **a three-step case costs roughly three times a flat one to verify**. This is a
nightly-job feature, not a per-PR gate.

UI flows are not read-only: a persistence check that renames an item leaves that rename in the app
for every later step, where the next step's agent and goal-holding client will both see it.
Convention: intermediate steps declare read-only flows (open, read, filter) and mutating checks are
reserved for the last step. Generation warns on any non-final step's `ui_flows` so the author
confirms they are read-only.

### Generated layout and per-step output

```
task.toml              [[steps]] with name, min_reward and split timeouts; multi_step_reward_strategy
environment/           byte-identical across the dataset, as for a flat case
steps/<name>/
  instruction.md       the step's prose plus the fenced JSON config for THIS step
  workdir/             only for a step with files
    step_files/<upload_id>/...
    setup.sh
  tests/               a complete copy of the standard verifier whose case.json holds this step's
                       expanded expectations
  solution/solve.sh    the oracle for this step: every prompt up to and including it, replayed
```

- There is **no top-level `instruction.md`, `tests/` or `solution/`**: harbor reads each step's own
  and would leave the top-level ones unread. In `separate` verifier mode a step's `tests/` *replaces*
  the task's build context rather than overlaying it, which is why every step ships the whole
  verifier. The Dockerfile copies the criteria (`tests/verifier/`) before `tests/case.json`, so
  steps declaring the same scoring dimensions share every layer beneath the case data.
- Each step's oracle replays the conversation **up to and including** that step. It has to: the
  structural gates hold a step answerable for every entry the trial has configured so far, so a
  single task-level script replaying the whole case into every step would fail each earlier step's
  turn gate. Harbor prefers a step's own `solution/` over the task's whenever the directory exists.
- `timeout_seconds` is the whole case's conversation budget and is **split across the steps** in
  proportion to their worst-case exchange counts, because harbor otherwise applies the task's agent
  timeout to every step. Each step's `[steps.agent].timeout_sec` is its share plus the verification
  budget plus grace, and each step restates `[steps.verifier].timeout_sec` so that the figure a
  reader of a step sees is the one that step gets rather than one inherited from the `[verifier]`
  block, which also configures the task-level verifier a stepped task never runs. Anything that must
  outlive one step (the proxy tunnel) is sized from the trial's whole lifetime instead, which is
  more than the conversation budget: between two conversations the trial also spends a step's
  evidence phase, its cleanup grace and its verifier container.
- That trial lifetime runs into **two ceilings a case config cannot raise**, and a stepped case has
  to fit inside both. The workspace every step shares is created on the `modal_eval` overlay, whose
  sandbox lifetime is 3h; generation warns when a case's worst case exceeds it, and there is no
  knob -- the fix is a shorter `timeout_seconds`, a shorter `verification_timeout_seconds`, or
  fewer steps. The box is capped separately by the run recipe's `--ek sandbox_timeout_secs=14400`,
  and it has to survive every step's agent run plus the verifier of every step but the last, so a
  long stepped dataset raises it by passing `--ek sandbox_timeout_secs=<n>` as an extra harbor arg
  (extra args pass through last and the later value wins). A flat case is nowhere near either.
- Per-step trial output: harbor moves the agent dir into `steps/<name>/agent/` after each step and
  empties the box's `/logs/agent` before the next one, so each step's `trajectory.json`,
  `state.json`, `usage.json`, `verification/` and `driver.log` land under that step. Their contents
  are **cumulative** (the conversation so far, the entries so far) except `driver.log` and
  `verification/`, which are genuinely step-local: the log sink is opened and closed per `run()`
  call, and each step's evidence is collected fresh.
- Anything a long-running box process writes goes to `/logs/artifacts/minds/` instead, which harbor
  collects after every step and never empties. The backend, the reverse tunnel and the proxy all
  start on the first step and outlive it, so a log of theirs under `/logs/agent` would be unlinked
  out from under its writer before step 1 even ran.
- Because a step's case file holds only that step's own turns while the entry records accumulate, the
  step config carries `step.entries_before`; the `all_turns_completed` gate holds a step answerable
  for that many entries plus its own.
- **`harbor trial regrade` does not support multi-step tasks.** Re-scoring a stepped trial means
  re-running it.

## Outcome verification

Quality criteria grade only how the agent *talks*, so an agent that chats beautifully and ships
nothing would outscore one that ships a working app in terse messages. A case that declares
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
- `deliverable` says what the case commissions. A block **without** one expands to no HTTP or file
  checks and no deliverable bundle, and the collector records only its always-on capture. With no
  `ui_flows` either, that leaves the outcome dimension to the judge reading the conversation -- a
  composition deliberately different from a deliverable case's even split between the judge and the
  programmatic checks, so **the two are not comparable score for score**. It exists for a stepped
  case's early phases, where the exit criterion is what the client and the agent agreed on rather
  than what is running; a flat case that commissions an artifact should say so. `ui_flows` are
  independent of `deliverable`: a block that declares them runs them and scores `ui_flows_completed`
  either way, which is how a later step probes what an earlier one delivered.
- `minds-app` is a **kind with implied checks**, not a hand-written check list: at least one
  *delivered* app registered in the workspace's `data/.state/apps.toml`, its supervisord service
  running, an HTTP 200 from each delivered app's root path, and the delivered repo captured as a git
  bundle. "Delivered" is narrower than "not pre-existing" -- see below. Optional
  `min_registered_apps`, `http`, and `files` entries *refine* that set rather than replacing it.
  Unknown kinds and unknown keys are rejected at generation time.
- `test_commands` are run in the delivered repo and recorded for the judge, but never gated: gating
  them would punish cases whose prompts never mentioned tests.
- `ui_flows` are natural-language flows through the delivered UI, each with a verifiable end
  condition -- see [UI flows](#ui-flows). A flow's reserved `script` field is *rejected* at
  generation time: scripted execution has no semantics yet, and accepting it would report a
  completed trial for verification that never ran.
- `fresh_env` is reserved and must be left unset. `fresh_env: true` is rejected at generation time,
  for the same reason: no fresh workspace is booted, so it would verify nothing.

The kind is expanded into its explicit check list **once**, in the generator (`expectations.py`), and
the expanded form is written identically into `instruction.md` and `tests/case.json` -- which is what
guarantees the collector cannot probe a different set of checks than the judge scores. The authored
form rides alongside as `authored_expectations`.

### Evidence, not live state

The verifier is a separate container that runs after the workspace has been destroyed, so everything
that needs the live app is captured at trial time into `/logs/agent/verification/` (declared as a
directory artifact) and the grade-time criteria score the *record*:

```
verification/
  manifest.json          # the index: every probe with a typed status
  file_inventory.jsonl   # {path, size_bytes, mtime} per file (snapshot excludes + .git, 20k cap)
  apps.toml              # verbatim registry capture
  services.txt           # supervisorctl status output
  repo_state.json        # HEAD sha, the base and dwt-tip shas, commit count, git status --porcelain
  deliverable.bundle     # incremental `git bundle <clone HEAD>..HEAD` -- the agent's own commits
  common_transcript.jsonl    # the workspace agent's common transcript, as `mngr transcript --format jsonl` wrote it
  workspace_trajectory.json  # the unmodified ATIF document `mngr transcript --format atif` built from it
  workers/agents.json        # `mngr list --format json` at collection time
  workers/<name>/            # per launched worker: trajectory.json, common_transcript.jsonl, reports/
  http/<check>_<n>_<app>.json  # per probe: status, headers, timing, body head (256 KB cap)
  flows/<slug>/log.jsonl # per UI-flow step: the verbatim page state, the action, the reasoning
  flows/<slug>/step_NNN.png  # a screenshot per step
  trace.jsonl            # every bridge command the collector ran, failures included
```

Every manifest entry carries a status where **`failed` means the workspace fell short and `error`
means the harness could not find out** (the bridge died, a probe timed out). That distinction is
load-bearing in both directions. `error` entries are excluded from the criteria they would have fed,
so an agent is never charged for a broken instrument, and a wholly unmeasurable `files`, `app`, or
`http` class errors the trial rather than scoring it (see
[Error versus zero](#error-versus-zero)). But a workspace whose app registry exists and
lists nothing is the agent shipping nothing, which scores as `failed` -- not waved off as evidence
the harness could not gather.

The registry, service, and inventory capture runs for *every* trial that got as far as a workspace,
including cases with no expectations, which is what makes a ships-nothing trial diagnosable. The
expectation-driven probes are skipped on trials that never finished, whose structural gates already
zero the reward.

The harness probes the app **as delivered** and never starts it. Minds' promise to the client is a
running app tab, so "built it but never started or registered it" is a delivery failure, not
something for the harness to repair.

`deliverable.bundle` is incremental against the eval-case commit the driver interposes (the template
clone with `system/vendor/mngr` overwritten), which is made with fixed author and committer dates so
that an identical tree always yields the same sha and the bundle can be unbundled onto a regenerated
clone. `repo_state.json` records that base sha and the template tip it was built from, so a replay
can regenerate and verify the base.

The evidence directory is created at setup, before anything can fail, and is always declared as an
artifact even when empty: harbor records a missing declared artifact path as a failed entry and
refuses to regrade any trial carrying one.

### What counts as a delivered app

Not every registry row is one, and nothing about a row's shape says which is which: the workspace
template's own apps (`system_interface`, `terminal`, `browser`, `files`, ...) register through
exactly the path a delivered app does. The collector (`evidence_collection.py`) subtracts three
kinds of row:

- **Pre-existing rows** -- what the workspace already served before the agent ran. A single
  `workspace_state` probe taken before turn 1 supplies both halves of that set, because neither is
  complete alone: the app registry as it actually stood (the only source that sees a template app
  registering its port from inside the program its supervisord entry runs -- its own entry point,
  or a launcher script -- as the terminal and the owner-exec and vm-exec daemons do), unioned with
  the names the workspace's own `system/supervisord.conf` registers through its `forward_port.py`
  invocations (`--name`, or the block's own program name for a `--manifest` registration), which
  covers a template app whose service had not registered its port yet. Measuring beats a hand-maintained
  name list, so the set stays correct for a dwt fork or branch that ships extra apps. The manifest
  records it as `preexisting_registrations`.
- **Rows the registry marks `internal = true`** -- machinery that forwards a port but has no page of
  its own to show, such as the owner-exec daemon, which answers 404 on `/` by design.
- **Throwaway "isolated instance" preview servers**, which register through the same
  `forward_port.py` path and leave their row behind when abandoned. They are excluded by reading the
  instance runner's own state under `data/.state/isolated-instances/`, not by matching name
  patterns: instance names are chosen by whoever starts them.

If the registry cannot be read the pre-existing set is **unknown**, not empty -- otherwise every app
the workspace booted with would count as delivered. The app, HTTP, and UI-flow entries are then
recorded `error` with reason `preexisting_unknown`, so the trial is unmeasured rather than scored
wrong, and `preexisting_registrations` is `null` (a different claim from a workspace that served
nothing). The registry and service capture still happens either way.

A registry name is not a supervisord program name -- a multi-port app registers extra origin rows
(`<name>-admin`) that no program owns. The service-health check joins a row to its program through
the `forward_port.py` invocations inside each `[program:*]` block of `system/supervisord.conf`, and
falls back to a program named exactly like the row, which covers a service that registers its port
at runtime instead of from the config. A row with neither is recorded as `no_supervised_program`:
the app was started by hand and would not survive a restart.

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

A flow's `name` names its evidence directory, slugified, and must be unique within a case *after*
slugifying -- so `Add Task` and `add_task` collide and are rejected at generation time. A flow may
also carry a `surface`. `origin` is the default and the only implemented one; the reserved
`minds-ui`, which would drive the Minds chrome and reach the app as an embedded iframe, is rejected
at generation time rather than silently falling back.

**The executor drives the app's forwarded origin from inside the box.** Flows run at the end of the
collection phase, inside its budget, in a headless Chromium the box launches for the flow -- its own
profile and its own CDP port, so no flow inherits another's cookies or storage -- navigating
`https://<label>.agent-<hex>.localhost:8431/` -- the app's own label on the workspace's agent-keyed
origin -- served by a `mngr forward` instance the driver owns. A host-side verification agent (the
decider's sibling) reads the page, decides one action, and a box-side step script performs it,
screenshots the result and reads the page back, all in a single box-local exec.
The reasoning stays host-side, so the loop is budgeted, logged, and attributable to harness spend.

This tests the app **through** the product's serving path -- forward proxy, SSH tunnel, label
origin, session cookie -- rather than under it. The browser is armed before its first navigation
with the trial's pre-auth cookie, scoped the way the proxy scopes its own: to the workspace's whole
origin family, so a flow stays authenticated wherever under it the app sends the browser. Elements
are addressed by ARIA role and accessible name, taken from Playwright's `aria_snapshot`, which is
also what the flow log records verbatim for the judge.

The verification agent's spend is reported as `metadata.verifier_agent_usage`, beside
`decider_usage` and never folded into the agent's own cost fields. It runs on the decider's model by
default; `--ak verifier_model=...` overrides it.

**Trial time records completion, never achievement.** A flow whose declared steps the agent
carried out is `passed` whatever the page showed; nothing here evaluates the `expect`. A step that
fails mid-flow -- an element that is not there, a click that hit nothing -- is recorded on that step
and the flow carries on, because the page below shows the truth and the grade-time judge reads it.

**Grading a product with its own machinery cuts both ways**, so app failures and executor failures
are kept apart. A flow is `failed`, and counts against the agent, when the workspace kept it from
finishing: the opening navigation to the app failed (including `action_timed_out`, the 30-second
page-load timeout), the 15-step budget ran out (`step_budget_exhausted`), the flow's own 600-second
`flow_deadline` passed, or nothing was ever registered to open (`no_app_to_open`). Machinery that
could not be driven is `error` instead, so the agent is not charged for it -- for example
`browser_launch_failed`, `cdp_connect_failed`, `forward_unreachable` (the proxy itself),
`tunnel_down` (proxy up, workspace leg dead), `tls_refused`, `step_bridge_failed`, `unknown_action`,
`step_error`, `workspace_unaddressable` (an agent id the proxy does not route, so no origin can be
addressed), `verifier_agent_failed`, and `timeout` when the collection phase's own budget ran out
mid-flow.
Those lists are illustrative; the complete vocabulary is the `REASON_*` constants in `ui_flows.py`
and `evidence_collection.py`, which also carry the registry-side reasons (`preexisting_unknown`,
`registry_absent`, `registry_unreadable`, ...).

The forward instance is the driver's own, not the one the headless minds backend may have spawned,
so it has a port and a pre-auth token the driver minted. It is configured at flag parity with minds'
own spawn -- `forward_instance_test.py` asserts that against minds' argv builder, so the two cannot
drift. It diverges deliberately in two ways: it adds a chosen `--port` (`forward_instance.py`),
and it drops `--embedder-origin` and `--reverse`, which shape only how minds *embeds* the app
(`forward_instance_test.py`).

## Scoring

All judging happens inside rewardkit, in the verifier container, over the recorded transcript and
evidence; `finalize.py` composes the trial's final reward from rewardkit's dimension scores
afterwards. `gates` and `quality` are scored on every trial and `harness_quality` on every trial the
claude harness ran; cases that declare `expectations` add `outcome`.

- **`gates`** -- structural: the trajectory parses, the agent engaged with distinct non-stub
  replies, all turns completed, the run did not time out. These zero the reward when they fail.
- **`quality`** -- the 1-10 likert judge criteria (`conciseness`, `nontechnical_language`,
  `nontechnical_status_language`, `proactive`) plus the programmatic message-length guard. The client
  reads the agent on two surfaces and the criteria are split along them: the first two judge the chat
  messages, `nontechnical_status_language` judges the progress timeline. `quality/prompt.md` holds the
  split the judge is given. The guard scores each message against the limit for its role in the turn
  -- a status line or the turn's answer -- rather than averaging the turn; `quality/message_lengths.py`
  holds what it measures and why. The driver separately records `average_words_per_turn` and the
  finer `average_words_per_message` in the trial metadata for observability; nothing at grade time
  reads either.
- **`harness_quality`** -- whether the workspace let the agent work at all, scored per scope: a 1-5
  likert judge (`main_harness_success` over the lead agent, `worker_harness_success` over every
  launched worker) and a programmatic criterion over the counted failure signatures
  (`main_harness_soundness`, `worker_harness_soundness`). `render_harness_report.py` holds what
  counts as a failure and why the dimension is separate. Every criterion in every dimension is framed
  so a higher number is a better outcome.
  It is **scored on the claude harness only**. The report the judges score is built by claude-shaped
  rules -- a skill invocation is the `Skill` tool, and two of the six signatures name claude's own
  skill and plugin vocabulary -- so on a pi-coding trajectory it comes out thin because those rules
  found nothing, not because the harness held, and
  the judge is told that a report with nothing in it is a 5: a false pass rather than a measurement.
  (The shell-level signatures are harness-blind and still fire, which is why the reports themselves
  are written whatever ran.) The verifier reads the harness off the graded trajectory: a captured
  document's own `agent.name`, and otherwise -- the driver's hand-built fallback names the *driver*
  there, and mngr writes `unknown` when it cannot resolve the agent -- the `harness` the
  [arm block](#what-the-arm-records) recorded. A trajectory that says neither is graded as claude,
  so a trial that merely lost its transcript keeps every dimension it always had. On any other
  harness the judge is not called and the dimension is left out of the trial's scores. Every trial's
  `reward-details.json` carries a `harness` block naming what ran and whether the dimension applied,
  so a dimension missing from the scores reads as one that does not apply here rather than one that
  failed to emit. The judges still see the conversation itself: what they grade it on is the agent's
  own messages, which carry no harness's vocabulary. The one place the judged transcript does read a
  harness's tool names is its executing-tool scan, which lists each harness's spelling of the shell
  (`Bash` and `BashOutput` on claude, `bash` on pi-coding) so that what a command printed reaches
  the progress timeline whatever ran it.
- **`outcome`** (expectation cases only; the generator omits the verifier's `outcome/` directory
  otherwise, so rewardkit never emits a partial score for it) -- one criterion per declared check
  class (`app_registered`, `http_expectations_met`, `files_expectations_met`,
  `ui_flows_completed`) plus a `works_as_expected` likert judge over the rendered expectations, the
  manifest, the conversation, and the flow evidence. The conversation is in there deliberately:
  `DECIDE_FROM_PERSONA` turns and goal entries are both free-form -- and a goal entry is a whole
  stretch of negotiation, not one line -- so a client who steers the build mid-conversation must be
  graded against the evolved ask.

`ui_flows_completed` scores COMPLETION: the fraction of measurable flows that carried out their
declared steps. It does not score whether the app did what a flow's `expect` describes. That is the
judge's ruling, made from the step log and the screenshots. Trial time collects; grade time
verifies.

### Reward composition

A stepped case is scored the same way, once per step, against that step's own expectations; the
trial's reward is then the last step's or the mean, per `reward_strategy`.

`harness_quality` then takes a fixed `HARNESS_SHARE` (0.2) of whatever the trial earned on quality and
outcome, leaving the parity between those two untouched: `reward = gates_all_passed ? (0.8 * earned +
0.2 * harness_quality) : 0`. Every dimension is read the same way, absent meaning 0.0. The rewards
composed here are the ones rewardkit just produced, not the ones stored when the trial was captured,
so regrading a trial older than a dimension grades it on that dimension and restates its reward --
which is what makes a regrade comparable to a fresh run rather than a reconstruction of an old one.

On a harness other than claude, where `harness_quality` is not scored at all, the composition
**drops** that share rather than redistributing it: `reward = gates_all_passed ? earned : 0`. This
keeps `reward` on the same range and with the same meaning on every
[arm](#harness-and-model-arms), at the cost of a claude arm and a pi arm weighting quality
differently -- acceptable because arms are compared within a harness first.

`quality = weighted mean(conciseness, nontechnical_language, nontechnical_status_language,
proactive, message-length guard)` -- likert
criteria normalized as `(raw - 1) / 9`, so raw judge scores stay recoverable (`raw = 9 * normalized
+ 1`; raw values are in `reward-details.json`). `reward` is that score, zeroed unless every
structural gate passed. For expectation cases it is an even split, `reward = gates_all_passed ?
(0.5 * quality + 0.5 * outcome) : 0`: a great app described badly and a great description of no app
are equally imperfect. The split is a constant, not per-case configuration -- per-case weights would
make rewards incomparable across cases.

Expectations that carry no `deliverable` register no HTTP, file or app criteria, so unless they
declare `ui_flows` -- which register `ui_flows_completed` either way -- their outcome dimension is
the judge alone rather than an even split with the checks. Those scores are on the same 0-1 scale
but are not the same measurement; see [Outcome verification](#outcome-verification).

The gate composition lives in the verifier's `test.sh` (`finalize.py`) because no rewardkit
aggregation expresses "binary gate zeroes a weighted mean"; a `reward.toml` could express the even
split on its own, but splitting the composition across two files buys nothing over the handful of
lines in `finalize.py` that state it next to its rationale.

Note how rewardkit weights a dimension, because it is easy to get backwards: every `.py` criterion
in a dimension directory is averaged into **one** programmatic reward of weight 1.0, and each
`judge.toml` is a **second** reward carrying its own `[judge].weight`. So the quality judge's
`weight = 3.0` buys equal weight *per criterion* across its three judge criteria and the one
programmatic guard, while the outcome judge's `weight = 1.0` is what makes it exactly half its
dimension however many programmatic criteria the case declares.

### What the judges read

Grade-time pre-steps rebuild the judges' inputs from the captured evidence on every grade, so
`harbor trial regrade` re-scores captured trials under the current rendering with no conversation
re-run:

- `judge_transcript.txt` (from `trajectory.json`): one `[USER]` block per `user` step, one
  `[AGENT · message N]` block per `agent` step with a message, so conciseness is judged per
  individual message rather than over a per-turn merge, and one `[PROGRESS · ...]` block per
  progress-timeline record, which is the second surface the client reads and appears in no `message`
  field (`render_judge_transcript.py` holds how those records are recovered). On the workspace's
  own document a message block is one inference; on the hand-built fallback (see
  [The trajectory](#the-trajectory)) it is one per turn, and that fallback carries no tool calls, so
  it renders no progress blocks. Both the quality judge and the outcome judge read it.
- `judge_flows_digest.txt` and a flat `judge_screenshots/` (from the flow evidence, which rewardkit
  cannot reach because it expands a listed directory exactly one level and never recurses). The
  digest carries, per flow: the declared steps, the `expect` the judge is to rule on, the completion
  status, the agent's own description of the final page (evidence, not a verdict), then every step's
  action, reasoning and page state. `judge_screenshots/` holds each flow's last four frames, up to
  24 in all, each under rewardkit's 1 MiB judge limit. Both are written unconditionally: rewardkit
  renders a listed path it cannot find as a visible `[not found]` block, while an empty listed
  directory renders *nothing at all*, which is why the digest states the screenshot count instead of
  leaving the judge to infer it.
- `harness_main.txt`, `harness_workers.txt` and `harness_failures.json` (from `trajectory.json` and
  each `verification/workers/<name>/trajectory.json`): the processed harness reports the
  `harness_quality` judges read, and the per-scope signature counts its programmatic criteria score.
  A raw trajectory runs to hundreds of KB and is mostly ordinary work, so each report keeps only the
  steps that bear on whether the harness held. `render_harness_report.py` holds the signature list
  and the rules for what is kept and what is counted.

### Error versus zero

A trial only records a score when the harness could actually grade it. Grading-infrastructure
failures error the trial instead, so they are never mistaken for a legitimate 0:

- a judge API or auth error, or rewardkit not producing a parseable reward file;
- a `gates` dimension carrying no readable criterion. The structural gates are programmatic, with no
  judge behind them, so a dimension that scored none of them is the verifier not having run rather
  than a trial that failed them -- and the reward composition cannot tell those apart, since both
  leave the trial gated shut. A gate that ran and said no is a measurement of the agent and stays a
  legitimate 0;
- a `tests/case.json` that is missing, unparseable, not a JSON object, or whose `expectations` is
  neither an object nor `null`. The generator writes that file into every task, so a broken one is
  the harness failing, and reading it as "this case declared no expectations" would grade a
  commissioned deliverable at quality-only weight. This check does not depend on how the trial went:
  the case file is part of the task, not of the run. A valid case file with `expectations` absent or
  `null` is the bare case and keeps grading quality-only;
- any of the following on an **expectations case whose gates all passed and whose `state.json`
  says the conversation finished** -- outside that window a partial or absent bundle is expected,
  and the structural gates already zero the reward:
  - no evidence bundle was collected, or its manifest is empty;
  - the outcome dimension produced no score at all;
  - a declared `files`, `app`, or `http` check class whose every recorded entry is an `error`. UI
    flows are exempt: every flow erroring usually means only that the executor was unavailable, and
    voiding the trial would discard its conversation-quality measurement over that.

A *partially* errored class still scores over its surviving entries, and `finalize.py` stamps an
`outcome_evidence` marker into `reward-details.json` recording how complete the measurement was. It
also stamps `timed_out`, true or false, on every graded trial; a timed-out trial scores 0 because
its structural gates fail.

## Token and cost accounting

A trial's cost is derived from the transcript the driver already collects (or from the in-box proxy's
log when one metered the trial), so a trial that timed out still accounts for what it spent. `agent/usage.json` carries the workspace agent's and the decider's
breakdowns, per model, over four non-overlapping token buckets (uncached input, output, cache read,
cache write) that Anthropic prices differently. The verification agent's and the transcript's own
figures are trial metadata only, not in that file.

**Whose spend is whose.** The workspace agent under test fills harbor's own fields:
`n_input_tokens` (cache inclusive), `n_cache_tokens`, `n_output_tokens` and `cost_usd` on the
trial's `agent_result`, plus the matching `final_metrics` on the ATIF trajectory -- both carry the
same resolved usage, which is the proxy's figures whenever a proxy metered the trial. The harness's
own models are reported separately and never folded into those fields -- the decider as
`metadata.decider_usage`, the UI-flow verification agent as `metadata.verifier_agent_usage`.

**Delegated work reaches transcript-sourced totals only when it was captured.** The events endpoint
serves main-session events only, so a subagent's turns never reach the transcript. Work handed to a
launched worker agent does, once the evidence phase has captured that worker's stream: each captured
worker is priced like the chat agent's own stream and summed into the transcript account
(`worker_launch_count` launches found in the captured transcripts, the chat agent's and each captured
worker's own, `worker_captured_count` of them brought out settled; a worker still running at capture
time, or one whose state could not be established, is summed but not counted, so the account stays
incomplete). A trial that delegated to a subagent, or launched a worker that could not be captured,
is marked `is_cost_complete: false`. Treat a flagged trial's cost as a lower bound, and never compare
it against an unflagged one.

**`--ak proxy=true` closes that gap**, by routing the workspace through a LiteLLM proxy the driver
runs inside the box, signed in with a per-trial key rather than the upstream credential. The
workspace's claude agents share one credential, so every call crosses that boundary, delegated ones
included: `agent/usage_proxy.jsonl` is the complete account and becomes the source for harbor's
fields, with the transcript's own figures kept in `metadata.transcript_usage`.

**The proxy meters the `anthropic` lane only.** Its address reaches the workspace through the
`ANTHROPIC_BASE_URL` line of the claude sign-in and nowhere else, and its model list is
Anthropic-only, so on any other [lane](#harness-and-model-arms) `--ak proxy=true` is refused at
construction rather than starting a proxy nothing would call. Non-Anthropic trials are priced from
the transcript, which is the path above: their `is_speed_observed` stays `false` and their
`is_cost_complete` follows the transcript rules.

**Fast mode changes the price, not the token counts.** Minds runs its claude chat agent in fast mode
by default, and fast mode bills the same tokens at twice the standard rate. That default is what the
default [harness config](#harness-and-model-arms) leaves in place; a config that names a model runs
standard unless it asks for `fast`, and pi-coding runs standard whatever the config. Fast mode is chosen per
request, so a model id alone does not determine a price, and only the proxy sees which tier served
one:

| `is_speed_observed` | `fast_message_count` | What `cost_usd` means |
|---|---|---|
| `true` | `0` | Exact: every request ran standard and is priced standard. |
| `true` | `> 0` | Exact: that many requests are priced at the fast-mode rate. |
| `false` | `0` | A floor. The tier was never observed, so everything is priced standard -- half the truth if the workspace was in fast mode, which is what the default harness config leaves it in. |

The table describes the two whole-log cases. `is_speed_observed` is true only when *every* record
carries the tier, so a proxy log that recorded it for some requests reads `false` with
`fast_message_count > 0` -- those requests are still priced fast, and the total is a floor only for
the rest. And "exact" assumes each request's model can serve the tier it asked for: a fast request
against a model outside `FAST_MODE_MODELS` is reported unpriced (`cost_usd: null`) rather than
silently halved.

`is_cost_rate_certain` reports that distinction as one boolean, on a separate axis from
`is_cost_complete`: that one asks whether all the traffic is in the total, this one whether the
traffic in it is priced at the rate it was billed at. **Fast mode is Opus-only**, and switching tier
invalidates the prompt cache, so a per-model comparison left at the default tier is not comparing
like with like.

**Pricing caveats.** Prices come from `mngr_usage`'s table, which `proxy_config.build_model_list`
also derives the in-box proxy's config from.

- `litellm_pricing_test` pins the four flat per-token buckets against litellm's own price map. The
  fast-mode multiplier is not in that map's shape: `mngr_usage`'s own `pricing_test` pins its 2x
  effect across every bucket, but nothing pins it against litellm.
- Every prompt-cache write is priced at the 5-minute rate, so a trial whose agent asks for the
  1-hour cache understates that bucket by 37.5%.
- The per-request `cost_usd` in `usage_proxy.jsonl` is always a standard-rate figure; only the
  trial's totals are tier-aware.
- An unpriced model, and a fast-mode request on a model that cannot serve fast mode, report
  `cost_usd: null` rather than a misleading `0`.

## Modal environments, and what a run leaves behind

Every trial creates its own Modal environment: mngr's Modal provider names it
`<MNGR_PREFIX><user_id>`, where `MNGR_PREFIX` comes from the staging activation inside the box
(`minds-staging-`) and `user_id` is the per-trial `MNGR__PROVIDERS__MODAL__USER_ID` the driver
mints. Teardown destroys the nested workspace *sandboxes*; it never destroys the environment.

**That leak is deliberate for a developer run.** The environment outlives the sandboxes inside it
and is where the state of a trial that went wrong is recovered from, hours or days later. Deleting
it at teardown would throw away the only thing left to look at. So removing them is opt-in:

```bash
just minds-evals-list-environments apps/minds_evals/jobs/<job>   # dry run: prints, deletes nothing
just minds-evals-cleanup-environments apps/minds_evals/jobs/<job>
```

Both read the names out of each trial's own `state.json` and touch nothing else. There is no
developer-facing sweep by name on purpose -- see [Cleaning up a scheduled run](#cleaning-up-a-scheduled-run).

A user id is `evals-<user_id_prefix><trial name>-<salt>`, held to 48 characters so the environment
name stays within the 64 mngr would otherwise truncate it to (truncation is a lossy left-slice with
no disambiguating hash, so a truncated name is one nothing can reconstruct afterwards). The
`evals-` namespace makes an eval's environment recognisable among the environments real staging
workspaces live in; the salt is what keeps two runs of the same trial apart. When the id has to be
squeezed it is the trial name that gives, never the prefix or the salt.

- `--ak user_id_prefix=<fragment>` prepends a fragment to every trial's user id, and so to its
  environment name. It is empty by default and validated against Modal's naming rules (lowercase
  alphanumerics and dashes, at most 25 characters) before any box boots. A scheduled run passes a
  per-run stamp, `ci-<YYYYMMDDtHHMMSSz>-`, minted by `minds-evals ci-user-id-prefix`, which is what
  lets that run's own environments be found afterwards. The timestamp is embedded in the name so the
  backstop sweep reads the age it acts on from the same string it matches its scope on, and the
  stamp is minted by the same module that parses it back so the two cannot drift apart.
- The exact environment name a trial created is recorded as `modal_environment_name` in
  `agent/state.json` and in the trial metadata, written during setup so a trial killed mid-run
  still says which environment to clean up.

Why the environments read `minds-staging-evals-...` rather than `minds-evals-...`: the prefix comes
from `MINDS_ROOT_NAME`, which `apply_bootstrap` re-derives on every minds and bridged `mngr`
invocation and validates against a fixed pattern (`minds`, `minds-staging`, `minds-dev-*`,
`minds-ci-*`). A root name of `minds-evals` fails it, and `minds-admin env activate evals` is
rejected by `DevEnvName` before that. Renaming would mean changing those validators in `apps/minds`,
which the whole app depends on, to buy a shorter name; namespacing the user id gets the same
recognisability for free.

## Checking a finished run

`minds-evals check-run <job_dir>` reads a finished harbor job directory and exits 0 only if the run
passed. A trial passes when all four hold:

- **it completed** -- harbor recorded no exception (trial-level or per-step), and the driver's own
  `state.json` says the conversation finished rather than timing out;
- **its structural gates held** -- every criterion of the `gates` dimension in
  `verifier/reward-details.json` scored above zero. A dimension with no criteria at all counts as
  not passed: that means the gates were never scored, which is a different claim from them holding;
- **nothing went unmeasured** -- no entry in `agent/verification/manifest.json` carries status
  `error`. That is the harness failing rather than the workspace falling short, which is why it
  fails the run where a `failed` entry does not (see [Evidence, not live state](#evidence-not-live-state));
- **it ran on the model its [harness config](#harness-and-model-arms) asked for** -- a trial whose
  `arm.harness_config` names a `model` and records `is_model_confirmed: false` fails, with a reason
  naming the model requested and the ones the record shows it answered on. `null` is neutral, since
  it is what the driver writes whenever it cannot tell, and a config that named no model is never
  judged on this.

**Judge scores are reported, never gated.** They are statistical and drift between runs; gating on
them would make the scheduled job fail for reasons a code change cannot fix.

`--summary-md <path>` writes a GitHub step-summary table -- one row per trial with its case, arm,
completion, gates, errored evidence, reward, judge scores, Modal environment, and the mngr and dwt
SHAs. The `arm` cell holds the harness half of the arm, with the pair in the mngr and dwt columns
beside it: the wrong-model reason when there is one, `-` when the trial recorded no arm at all, and
otherwise the lane, the requested model (the word `default` when it asked for none) and, when one
was requested, `confirmed` or `unconfirmed`: `anthropic haiku confirmed`,
`api-key anthropic/claude-haiku-4-5 unconfirmed`, `anthropic default`. `--summary-json <path>` writes
the same rows as JSON with `is_passed`, `job_name`, `modal_environment_names` (exactly the list the
cleanup below acts on), and the harness half as its own fields (`lane`, `requested_model`,
`is_model_confirmed`, `wrong_model_reason`). An oracle run
(`harbor run -a oracle`) is checkable the same way: it writes a `state.json` and a reward, and its
fabricated evidence carries no `error` entries.

## Cleaning up a scheduled run

`minds-evals cleanup-environments` deletes environments in two scopes, both honouring `--dry-run`:

- `--job-dir <job_dir>` deletes exactly the environments that job's trials recorded, and nothing
  else. This is what the `just` recipes above run, and what a scheduled job runs when its job
  directory survived. It reads each trial's `state.json` on its own and skips, with a warning, a
  trial it cannot read -- deliberately more forgiving than `check-run`, which refuses a job it
  cannot parse: a trial truncated by the crash being cleaned up after must not take the other
  trials' environments down with it.
- `--sweep-prefix <prefix> --older-than-hours N` is the backstop for a run whose job directory did
  not. It lists the workspace's environments through the Modal SDK, keeps names starting with the
  prefix whose embedded `ci-<YYYYMMDDtHHMMSSz>` stamp is older than the cutoff, and deletes those.

  Two guards bound it, and they do different jobs. A prefix that does not contain `ci-` is
  **refused**, not silently narrowed, which stops an operator aiming the sweep at something broad.
  The **timestamp is what keeps a developer's environment out**: a prefix match alone proves nothing,
  because the trial name follows the `evals-` namespace directly, so a developer's run of a case
  whose id begins with `ci-` lands under the CI prefix with no `user_id_prefix` at all. Such a name
  carries no stamp, so it is skipped rather than deleted -- as is any matching name with no parseable
  timestamp, since with no age to judge, deleting on the prefix alone would take out a run that is
  still going.

Deletion cascades the apps and volumes inside the environment. An environment that is already gone
is not an error -- a second cleanup pass over the same job is a no-op. Deleting needs *manage*
access to the Modal workspace: the CI token has it, and a developer token without it sees each
deletion reported as failed (the environment stays; ask a workspace admin to remove it).

## Notes

- A dataset builds **one** box image, keyed on the mngr SHA and the Dockerfile together and cached
  as a whole: `modal.Image.from_dockerfile` compiles the file into a single build with no
  per-instruction cache. Cold that is about two and a half minutes on Modal's builders (apt ~41 s,
  `uv sync` ~30 s, playwright ~27 s, pnpm ~17 s, ~25 s of fixed Modal overhead); an unchanged
  Dockerfile plus context is a hit at ~10 s to a running sandbox. One changed byte in the staged
  mngr clone re-runs all of it, so reordering instructions saves nothing. Keep per-case data out of
  `environment/` or the cache key diverges and every task pays its own build.
- Debugging: `uv run --project apps/minds_evals harbor task start-env -p <task> -e modal -i`, then
  `modal shell`. harbor's Modal provider opens no tunnels, so there is no live desktop URL, but the
  box still runs x11vnc/websockify for in-sandbox use.
- Cleanup: the driver destroys its nested workspace sandboxes in a `finally` block
  (`mngr list --ids | mngr destroy - --force`, scoped to the trial's own USER_ID); the nested
  sandboxes' `modal_eval` 3h timeout is the backstop if the runner dies hard. The Modal
  *environment* those sandboxes lived in is left alone -- see
  [Modal environments, and what a run leaves behind](#modal-environments-and-what-a-run-leaves-behind).
- This app contains async code (`driver.py`, `minds_bridge.py`): harbor's agent and environment
  APIs are async, so the ratchets that normally forbid async/asyncio carry nonzero baselines here.

## Scheduled CI

`.github/workflows/minds-evals-scheduled.yml` runs this eval nightly at 11:00 UTC as a **matrix of
arms**: each `(mngr, default-workspace-template)` pair times each selected
[harness config](#harness-and-model-arms). One cell is one arm, and the cell is the unit of
everything below -- what runs, what is skipped, what is reported, and what is remembered green.

Two pairs are evaluated by default:

- **main** -- both repos at `main`. Answers "is what we are about to ship healthy?".
- **released** -- the `minds-v<version>` tag named by the `stable` channel's `fallback_branch` in
  `apps/minds/release-channels.toml`, on both repos. Answers "is what users are running healthy?".

Both pairs run at once; a red one does not cancel the other. Every ref is resolved to a SHA once, up
front, so a pair cannot drift between dataset generation and the run. A pair whose refs do not
resolve -- a `minds-v<version>` tag that is not yet on both repos, say -- is reported as unresolved
and left out of the run, which also does not cost the other pair its answer. Dependencies are
installed from `apps/minds_evals/uv.lock` or the run fails: the only thing meant to move between two
nights is the pair of SHAs.

<<<<<<< HEAD
Each pair runs two passes. The **oracle pass** replays the canned transcript: it exercises the box
image build, generation, the verifier container and grading without booting Minds or paying for the
agent, and it gates the expensive pass. Cheap is not free -- grading is the verifier's judge call, so
an oracle-only run still costs one judge pass per case (which is why an oracle run asserts
`reward >= 0.8` rather than exactly 1.0). The **live pass** is the real eval, at a concurrency equal to
the config's case count so every case runs in one wave. `minds-evals check-run` decides both: it
passes only when every trial completed, no trial carries a harness `error` status, the structural
gates hold, and no trial that asked for a model is recorded as having answered on another.
**Judge scores are reported, never gated** -- they are statistical, and one run's number is not a
regression signal.
=======
### Which harness configs a run evaluates
>>>>>>> origin/main

`configs/harness_configs.json` is the checked-in list of named harness configs, and it is where the
spend decision lives: every entry carries an `is_nightly` flag, and a schedule runs exactly the
entries that set it. `default`, `haiku` and `pi-gpt-5-mini` are nightly; `pi-haiku`,
`pi-glm-4.7-flash` and `opus-standard` are not, and run only when a dispatch names one. The file's
order is the order the run's cells are decided in, and so the order of the grid's columns in the
Slack report -- which is why arms worth reading against each other, `haiku` beside `pi-haiku`, are
listed side by side. Everything else in an entry is one of the run line's own
[kwargs](#harness-and-model-arms), with the same default an unset kwarg has, so `default` -- a lane
and nothing more -- is the product exactly as it ships.

A name matches `^[a-z0-9][a-z0-9.-]*$` (dots are in, because a model version is part of what names
an arm), is at most 30 characters, is unique in the file and is never `oracle`: it labels a harbor
job, a concurrency group, an artifact and a line of the Slack report, beside the oracle's own. Every
entry is validated through the driver's own kwarg parsing on the free `resolve` job, selected or
not, so a config the driver would refuse at construction fails before any paid runner starts, and a
unit test validates the checked-in file on every test run.

### The jobs

- **`resolve`** (free) freezes each pair's refs to SHAs, lists the green markers once with
  `gh cache list --key minds-evals-green-`, and runs `minds-evals ci-matrix` to decide the cells:
  which pairs run, which cells run, and what each cell's run line and marker key are.
- **`oracle`**, one entry per pair with at least one running cell, replays the canned transcript: it
  exercises the box image build, generation, the verifier container and grading without booting
  Minds or paying for the agent, and it gates the expensive pass. Cheap is not free -- grading is the
  verifier's judge call, so an oracle-only run still costs one judge pass per case (which is why an
  oracle run asserts `reward >= 0.8` rather than exactly 1.0). **The oracle pass boots no workspace,
  so it is independent of the harness config**: it runs once per pair rather than once per cell, and
  a pair's live cells run only after its oracle passed.
- **`evaluate`**, one entry per running cell, is the real eval, at a concurrency equal to the
  config's case count so every case runs in one wave. A cell fetches only its own secrets, downloads
  its pair's oracle summary and refuses to start unless that oracle passed, regenerates the same
  dataset at the same SHAs (so the image build is a Modal cache hit), runs `just minds-evals-run`
  with its harness config's `--ak` flags appended, writes its green marker as soon as the live pass
  has been checked green, and only then deletes the environments it recorded and uploads its
  artifacts, so a cleanup the Modal API refused turns the job red without costing the arm a re-run.
- **`notify`** posts one Slack message per pair through `minds-evals ci-report`, and writes the
  same reports to the run summary.

`minds-evals check-run` decides both passes: it passes only when every trial completed, no trial
carries a harness `error` status, the structural gates hold, and no trial that asked for a model is
recorded as having answered on another -- so a cell goes red on a model switch that did not take.
**Judge scores are reported, never gated** -- they are statistical, and one run's number is not a
regression signal.

The oracle job's concurrency group is `minds-evals-oracle-<pair>` and a cell's is
`minds-evals-<pair>-<config>`, so two runs never evaluate the same arm at once. The harbor job names
are `<pair>-oracle-<run_id>` and `<pair>-<config>-live-<run_id>`.

### Green markers, and what they promise

A cell that passes end to end is recorded green in an `actions/cache` marker keyed on
`minds-evals-green-<pair>-mngr-<mngr sha>-dwt-<dwt sha>-cfg-<config path slug>-hc-<harness config name>-<12-hex digest>`,
and the next night skips it. The digest is over the harness config's own kwargs, so editing a config
re-runs its cells under the same name and leaves every other cell green. A cell is skipped when its
exact key is among the markers the run may restore -- those saved on the run's own ref or on the
default branch -- and a pair whose every cell is skipped runs no oracle pass either. A red cell
saves nothing, so it retries on the next slot. Dispatch with `force` to run a cell anyway. List the
markers with `gh cache list --key minds-evals-green-`; the Caches web UI cannot filter by key prefix.

The lookup is that one `gh cache list` call rather than an `actions/cache/restore` step per cell,
because the action cannot be looped over a matrix that is decided at run time.

Nothing that comes from the branch the workflow runs on is in that key -- not the eval config's
contents, and not the eval harness in `apps/minds_evals` (the driver, the gates, the verifier) -- and
neither is the live staging tier every trial boots. **A green marker says an arm was verified once,
against whatever harness and tier existed then.** The `main` pair picks up a harness change anyway,
because its SHA moves whenever `apps/minds_evals` does. The `released` pair does not: its refs are
the release tag, so between releases it is verified once and skipped every night after -- a new gate,
or a staging regression, is not seen there until the next release. Dispatch with `force` to
re-verify any of that.

### Dispatching a run

`workflow_dispatch` inputs:

- `pair` -- `both` (default), `main`, or `released`.
- `mngr_ref` / `dwt_ref` -- a one-off pair. Setting either replaces the pair selection with a single
  `custom` pair (branch, tag, or full SHA; the unset side defaults to `main`).
- `harness_configs` -- comma-separated harness config names. Empty (the default, and what a schedule
  and a branch push get) runs the nightly set; a name that is not in the file fails the `resolve`
  job.
- `config` -- which eval config to generate the dataset from; empty (the default) uses
  `eval-config-small.json`. Its persona count becomes the run concurrency.
- `force` -- evaluate a cell even when its green marker says that arm was already verified.
- `skip_live` -- run only the oracle passes. This is the cheap way to test changes to the workflow
  itself; it writes no green marker, because it verified nothing about the live path. Every cell is
  decided as if `force` were set, so a night when nothing moved still has an oracle pass to run
  rather than skipping into a report of a matrix it never exercised.

GitHub accepts a `workflow_dispatch` only for a workflow file that is already on the default branch,
so a change to the workflow cannot be dispatched from the branch that carries it. Push that branch
under `minds-evals-run/` instead (`git push origin HEAD:minds-evals-run/<anything>`): the push runs
the main pair against the nightly configs, oracle pass only, unless the head commit message carries
`[live]`, in which case the live pass runs too. An ordinary feature branch never triggers it. Every
cell of a push is decided as if `force` were set, whether or not it asks for the live pass, so a
test push always has something to run rather than skipping into a report of a matrix it never
exercised; a push that stops at the oracle writes no green marker, and its Slack report says
`oracle only`. A push that does write markers writes them under its own ref, which no nightly can
restore.

### Results

The step summary of each pass carries its check-run markdown (`resolve` writes the arms table and
`notify` the Slack report instead), and artifacts are kept for 14 days: per pair,
`minds-evals-summary-<pair>` and `minds-evals-jobs-<pair>-oracle-<run_id>` from the oracle pass; per
cell, `minds-evals-summary-<pair>-<config>` and `minds-evals-jobs-<pair>-<config>-<run_id>` from the
live pass. A summary artifact holds `oracle-summary-<pair>.{md,json}` or
`live-summary-<pair>-<config>.{md,json}`, named so that `notify` can merge every one of them into a
single directory. The `-jobs-` artifacts hold the full job directories minus `agent/snapshots/` --
the workspace tarball is ~90 MB of a trial's ~92 MB.

The `notify` job posts **one Slack message per pair**, rendered by `minds-evals ci-report` -- the
two pairs answer different questions, and a reader acts on one of them at a time. A message opens
with a header naming the pair and its verdict (passed, failed, skipped, not evaluated, or broken:
the worst of the pair's oracle pass and its cells), then a line carrying the pair's refs, SHAs and
oracle verdict beside the run's duration and trigger. It posts under `:big_brain:` when every arm it
reports came out green -- every cell passed, every cell was skipped as already green, or an
oracle-only run's oracle passed -- and under `:brainless:` for anything else, so the verdict reads
off the channel list before the message is opened. The icon answers "are the arms good?", so a run
whose arms all passed but whose job went red keeps `:big_brain:`; the warning in the message itself
is what says the job broke.

Under that is the **grid**: one column per harness config, in matrix order, and one row per case. A
grid cell places the trial's reward on the absolute 0..1 scale as a coloured square -- red under
0.25, orange under 0.50, yellow under 0.75, green at or above it -- followed by the reward itself
and, where the trial did not pass, a bold cross. The cross covers every way a trial can fail: it did
not complete, a gate failed, evidence went unmeasured, or it answered on the wrong model. The colour
therefore means one thing throughout and never competes with the verdict, and a legend under the
grid states the bands and the cross, derived from the bands themselves so the words cannot drift
from the cells. `:heavy_minus_sign:` is a pass that was never attempted, skipped because it is
already green or gated off by a failed oracle, and `:grey_question:` a cell with no reward to
place: a pass whose story cannot be told, with no summary at all because the job died before grading
or one that could not be read, a case a graded column has no trial for, or a trial that was never
graded. An oracle-only run has a single `oracle` column, and a pair that graded nothing -- skipped,
unresolved, or broken throughout -- has no grid.

Under the grid, **the trials that did not pass** are listed in a table of config, case and the
reason `check-run` recorded. It is drawn only when something failed: a heading over an empty table
reads as a measurement that went missing rather than as a night with nothing to report.

Below that, one collapsed container, `judge scores`, holds every graded trial of the pair in a
single table -- config, case, reward, a column per judge criterion, then what became of the trial --
rather than a table per harness config, so a criterion can be compared straight down its own column.
The criteria are the union across every arm: a criterion only one config was scored on still gets a
column, and the arms that were not scored on it print `-` rather than a zero. A column is headed
with the bare criterion name, and with `<dimension>: <criterion>` only where two dimensions scored
criteria of the same name; which dimension scored which criteria is stated once in a legend above
the table (`quality: conciseness`, `outcome: works_as_expected`), because the dimension is what says
whether a score is about the product or about the harness that drove it.

Slack refuses the whole message over 20 cells in a table row or 10,000 characters across the cells
of all its tables, so the message keeps itself under both. Every cell is clamped to 120 characters
first -- a case id and an incompletion reason are both unbounded -- then a pair scored on more than
16 criteria keeps the first 16, and the judge table, the only part that grows with cases times
configs times criteria, is cut to whole rows of whatever budget the grid and the failures table left
it. Each cut says so out loud: in the legend above the table, or, where no row fit at all, in a line
where the container would have gone.

A **details** block follows with whatever belongs to no arm's own row: one line per skipped, broken
or not-evaluated cell, one per failing trial of a pass with no grid column of its own (a failed
oracle on a pair whose cells ran), and one per passing trial whose requested model nothing confirmed.
It is budgeted at 2900 characters, under the 3000 a Slack section holds, and cut on a line boundary
when it overruns. Links to the run's logs and artifacts close the message.

Only the blocks an incoming webhook accepts are used, which is what `table` and `container` are
doing the work of. A webhook refuses `data_table` outright -- a minimal one is answered with
`400 invalid_blocks` -- so the sorting and paging a data table would bring are unavailable until the
notify job posts as an app with a bot token rather than through a webhook, and `markdown` is refused
as well.

The report covers the run as a whole, not only the trials: a run whose every cell passed but whose
jobs went red or were cancelled -- a cleanup that could not delete the run's Modal environments, say
-- is flagged rather than reported as a clean success, and a run that resolved nothing at all gets a
single `broken` message naming the three job results instead of pairs.

Every message also carries a plain-text rendering of itself, with the grid, the failures and the
judge table each as a fixed-width fence. The grid's fence says `ok`, `FAIL`, `-` and `?` in place of
the square, because Slack renders no emoji inside a fence, and the judge fence carries the same
dimension legend on the line above it. `notify` writes that
into the run summary and posts it on its own, with a warning, if Slack refuses the message's blocks.
It reads the webhook from Vault at `mngr/ci/SLACK_MINDS_EVALS_WEBHOOK`; without it the job warns and
the run summary is the whole report. Either way the run is never red because of its notification.

Every job fetches the credentials it needs and no others, from the same Vault role as the other CI
jobs. `resolve` needs none and `notify` only the webhook above; `mngr/ci/ANTHROPIC_API_KEY` and the
`mngr/ci/MODAL_TOKEN_ID` / `mngr/ci/MODAL_TOKEN_SECRET` pair go to every job that runs a pass, the
oracle and the cells: the judges spend the Anthropic key on every pass and the decider on every live
one, and CI writes the Modal tokens into a throwaway `~/.modal.toml` because the driver parses one.
That token belongs to the imbue Modal workspace, the same one a developer's `[imbue]` profile puts
their own runs in; the staging Minds tier the box activates is reached over HTTP, so the token's
workspace is independent of it. A cell whose harness config reads its lane's key from another
variable fetches `mngr/ci/<key_env>` on top of that, and only that cell does -- a lane's key is
never exported into a cell that does not sign in on that lane. So the `pi-gpt-5-mini` cell needs
`mngr/ci/OPENROUTER_API_KEY` to exist in Vault; without it that cell fails at the fetch step, before
a box is built, and the other cells are unaffected.

### CI environments and cleanup

A CI run passes `--ak user_id_prefix=ci-<YYYYMMDDtHHMMSSz>-`, so every Modal environment it creates
is attributable to one run and one wall-clock time. Teardown is three-layered:

1. The driver destroys its own nested workspace sandboxes in a `finally` block.
2. `minds-evals cleanup-environments --job-dir <job dir>` deletes exactly the environments that job
   recorded, and runs even when the pass failed. This layer runs in **every** job, oracle and cell
   alike, and collects the pass it is part of.
3. `minds-evals cleanup-environments --sweep-prefix <ci prefix> --older-than-hours 8` is the backstop
   for a job that died before recording anything. It runs in the **oracle** job, once per pair, and
   deletes only names carrying a scheduled run's `ci-<timestamp>` stamp. **The cutoff is `now - 8 h`
   and the budget of every job that creates an environment is 3 h 20 m, so the sweep never collects
   the run it runs inside, nor the cells that run after it** -- an earlier run's leak is collected
   by a later run, usually the next night's. That is the point of the 8 h: it puts every
   still-possible eval, including the cells running alongside, and every developer's work out of
   reach.

Cancelling a job (or hitting its timeout) SIGKILLs harbor rather than unwinding it; the boxes it
leaves then ride their own timeouts -- 3 h for the nested workspace sandboxes, and for the harbor
sandbox 4 h in a live pass, which is where `just minds-evals-run` sets `--ek sandbox_timeout_secs`
(the oracle pass calls harbor directly and takes its default) -- and the cleanup step still runs.
