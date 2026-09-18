# minds-evals

Harbor-based Minds persona evals. Each persona case in an eval config becomes one
[harbor](https://github.com/harbor-framework/harbor) task; a run drives real multi-turn
conversations against real Minds workspaces on Modal and grades the transcripts with a rewardkit
verifier. It replaces a bespoke pre-harbor harness; every config that harness accepted still reads
unchanged, and the schema is a superset of it: a config that adopts a goal entry (below) runs here
only.

The vocabulary this README uses (case, trial, arm, box, harbor step versus flow step, verifier
versus verification agent, and the rest) is defined in the [glossary](docs/glossary.md). Keep it
current in the same PR as the change: a new term gets an entry naming the module, class or key
that carries it, a rename updates or retires its entry, and a word that acquires a second meaning
goes into the glossary's collisions section. An entry marked *Earmarked* names a term already
slated to change, so do not build new names on it.

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
4. It then **creates the workspace's chat** through `/api/chats/create`, named after the
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
- A run on another provider lane also needs that lane's own key -- `OPENAI_API_KEY` on the `openai`
  lane, `OPENROUTER_API_KEY` on the `openrouter` lane, `<PROVIDER>_API_KEY` on the `api-key` lane.
  See [Harness and model arms](#harness-and-model-arms). `ANTHROPIC_API_KEY` stays required whatever
  the arm, because the decider, the judges and the UI-flow agent call the Anthropic API on every
  trial; the lane's key is the *workspace's*, and is a separate concern from the harness's own spend.
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
  app or the monorepo packages it depends on. With no args it runs two pytest sessions, each
  across two xdist workers and each held to CI's per-session time limit: the tests marked
  `chromium` (every test using the `chromium_path` fixture, marked automatically) in one, the
  rest of the suite in the other, with coverage combined across both. Given args (a path, a node
  id, a `-m`) it runs the one session they select, on the same two workers; add `-n 0` to the
  args to run it in-process, as `--pdb` and `-s` need. Tests run in parallel, so a test must not share
  a fixed path, port or other process-wide state with another: take directories from `tmp_path`
  and ports from the OS.
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
claude harness), `openai` (codex), and `api-key`, `openrouter` and `opencode-go` (all pi-coding);
`harness_for_lane` in `data_types.py` is that mapping. Each of them offers a pasted-key sign-in,
which is the only kind a run can drive; the workspace's
one other lane, `google` (antigravity), offers only browser flows on a PTY and so needs a person at
it.

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
provider's default at standard speed (`claude-opus-4-8` on an Anthropic key), and on codex the first
entry its picker would show, which moves with the codex version the template pins. It still records
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
`off` through `max` per model. codex offers whatever its account's catalog lists, which on an API
key is the one bundled into the codex release the template pins, and so changes with that pin
(codex 0.154.0, read 2026-09-13): `gpt-6-astra`, `gpt-5.6-sol` and `gpt-5.6-terra` with efforts
`low` through `ultra`, `gpt-5.6-luna` with `low` through `max`, then `gpt-5.5` and `gpt-5.2` with
efforts `low`, `medium`, `high` and `xhigh`. Its `fast` axis is the `priority` service tier, which
the catalog offers per model: every id above has one except `gpt-5.2`. The driver validates none of these
strings itself: the endpoint's own 400 is the validation, and the trial's reason quotes its
detail (to 300 characters), so a typo in a catalog id is legible from the trial listing.

```bash
DS=/tmp/minds-evals/datasets/small

# the default harness config on the anthropic lane
just minds-evals-run $DS default 3
# opus at standard speed, for cost comparisons against the other arms
just minds-evals-run $DS opus-standard 3 --ak model='opus[1m]' --ak effort=high
# a cheap claude config
just minds-evals-run $DS haiku 3 --ak model=haiku --ak effort=medium
# pi-coding on an Anthropic key
just minds-evals-run $DS pi-haiku 3 --ak lane=api-key --ak key_provider=anthropic \
  --ak model=anthropic/claude-haiku-4-5 --ak effort=medium
# pi-coding on OpenRouter
OPENROUTER_API_KEY=... just minds-evals-run $DS pi-gpt-5-mini 3 --ak lane=openrouter \
  --ak model=openrouter/openai/gpt-5-mini --ak effort=medium
# codex on the 5.6 line's frontier model, at that model's own default effort
OPENAI_API_KEY=... just minds-evals-run $DS codex-sol-low 3 --ak lane=openai \
  --ak model=gpt-5.6-sol --ak effort=low
# codex on the 5.6 line's everyday model, at that model's own default effort
OPENAI_API_KEY=... just minds-evals-run $DS codex-terra 3 --ak lane=openai \
  --ak model=gpt-5.6-terra --ak effort=medium
```

One dataset serves every harness config, and each run needs its own job name, because harbor refuses
to reuse one. The `openai` lane also needs a workspace template that offers it a pasted-key sign-in,
which default-workspace-template `main` does from `ea2fbc2cd` (2026-09-09) on: a dataset pinned to an
older template (the `dwt_branch` its [eval config](#eval-config) pins, or `--dwt-ref` at generation)
gets its sign-in refused on every trial, naming the lane.

**The named harness configs** live in `configs/harness_configs.json`: `default`, `haiku`,
`pi-haiku`, `pi-gpt-5-mini`, `pi-glm-4.7-flash`, `codex-sol-low`, `codex-terra`, `codex-astra-low`
and `opus-standard`, each a name, an `is_nightly` flag and the kwargs above. It is the list the
[scheduled CI](#scheduled-ci) composes its cells from, and the place to look for a config that is
known to work -- an entry's kwargs are exactly the flags to append to a `just minds-evals-run`
line to drive the same arm locally, and the entry's own name is a job name that says which arm the
run was.

### The lane's key

The driver reads the workspace's key from the variable `key_env` names. Left unset, it derives one:
`ANTHROPIC_API_KEY` on the `anthropic` lane, `OPENAI_API_KEY` on the `openai` lane,
`OPENROUTER_API_KEY` on the `openrouter` lane, and `<KEY_PROVIDER>_API_KEY` on the `api-key` lane,
upper-cased with dashes turned into underscores (`key_provider=openrouter` derives
`OPENROUTER_API_KEY`, and `key_provider=ant-ling` derives `ANT_LING_API_KEY`). The derivation is a
convenience, not a contract with the workspace template: the template names the variable per
provider and does not always follow the pattern (`google` reads `GEMINI_API_KEY`), and
`opencode-go` has no derived default at all. Name the variable with `key_env` in those two cases. A
variable that is unset stops the run at construction, naming the variable and the lane; a lane
outside the table above is refused there too, and no `key_env` makes one runnable.

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
  product's own model choice (`POST /api/chats/<chat_id>/model`).
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
  under the naming of the lane's harness -- and the lane is what picks the rule, since a bare `haiku`
  and a bare `gpt-5.5` are the same shape and mean different things. On `anthropic` a catalog id is
  an alias that a four-entry table translates (`haiku` reports as `claude-haiku-4-5-20251001`), and
  an id the table does not carry is untranslatable. On the pi lanes a `<provider>/<model>` tag
  reports as itself minus its first segment (`anthropic/claude-haiku-4-5` as `claude-haiku-4-5`), and
  an id with no `/` is untranslatable. On `openai` a codex id reports as itself, so nothing there is
  untranslatable. The comparison follows the lane too: a claude or pi name is matched by prefix,
  because the harness decorates it (claude appends a release date), while a codex name is matched
  exactly -- a config asking for `gpt-5.5` must not be confirmed by a chat that answered as
  `gpt-5.5-mini`. A config that named no model has nothing to confirm, so every default-config trial
  records `null`. An untranslatable id, and a trial that observed no model at all (its transcript was
  never captured, or it gave up before a turn), leave it `null` as well, never `false`: silence is
  not evidence of a wrong model. On a proxied `anthropic` trial the proxy's `per_model` is the ground
  truth and the confirmation is computed from that instead -- the requested model has to be there,
  and every other row has to be the greeting's, since the proxy cannot tell which turn served which
  request.

**A codex trial observes no model at all.** mngr's codex transcript emitter writes no per-step
`model_name`, so a trial on the `openai` lane records `observed_models: []`, an empty
`welcome_model`, and `is_model_confirmed: null` however well the switch went. Read
`model_choice_switch` on such a trial: `applied` means the endpoint took the model the config asked
for, and it is the only thing the harness config says about it. The workspace's own message feed does
name the model each codex turn ran on -- it is the token count beside it that is missing -- so the
trial's conversation carries a second record the harness config does not read. Such a trial cannot
account for its spend either, for a separate reason and with a separate owner (see
[Token and cost accounting](#token-and-cost-accounting)).

A trial that silently ran on the wrong model is the failure worth catching, so read
`is_model_confirmed` before reading a comparison. [`check-run`](#checking-a-finished-run) fails a
trial whose `false` says it answered on another model, so a switch that did not take breaks the run
loudly instead of skewing a comparison quietly.

### Comparability

An arm changes the system under test, in either half. A run with `fast=false` is not comparable to
a run that left the template's fast mode on -- which is what the default harness config does -- and
a pi-coding run is not comparable to a claude run on any dimension but cost. A codex run is not
comparable on cost either: its spend is unknown rather than measured, so it belongs in no cost
comparison at all (see [Token and cost accounting](#token-and-cost-accounting)). Two arms that
differ in both their pair and their harness config attribute nothing to either, so vary one at a
time. Grading differs too: `harness_quality` is not scored on a harness other than claude, and the
reward drops its share (see [Reward composition](#reward-composition)). Version or flag result sets
at the arm, the same way they are versioned at the adoption of a goal entry.

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
  recorded: the workspace UI feed the driver polled, then the detail payloads it read of that feed's
  tool calls, followed by one record per decider-model call with the message it produced (the
  trajectory's `extra.minds_evals.decider_turns` carries the same calls without their text). Every
  record names its own kind under `type`: a polled event is `feed_event` and keeps the workspace's
  event verbatim under `event` -- which has a `type` of its own (`assistant_message`,
  `user_message`, ...) and is why the kinds are not flattened together -- a detail payload is
  `event_detail` and carries the `event_id` it belongs to beside the chat app's payload under
  `detail`, and a decider call is `decider_message`. Written host-side after every turn and again once the
  evidence phase is done, never mirrored into the box, and touched by no grade-time reader. Reach for it when the
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

Two transcripts of the workspace agent exist: the workspace UI feed (`/api/chats/<chat_id>/events`),
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
  in its own stream, captured one by one with a preservation-aware `mngr transcript` lookup (which
  reaches a worker destroyed after finishing, and retries without it on a workspace mngr too old to
  know the flag), and embedded in `trajectory.json` under the launching call as ATIF
  `subagent_trajectories` with `subagent_kind: "mngr"` and an `extra.worker` block. A worker a
  complete listing no longer names, whose stream the capture still produced, is recorded as
  destroyed; anything less conclusive is recorded as unknown. Launches are followed three levels deep: the chat agent's workers, their
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
guessed at. That message rides in the marker's own `extra.minds_evals.opening_message` (empty when
the step ended before the client said anything), so the placement is checkable against the
conversation from the document alone. A task without steps has nothing to divide and gets no marker.

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
rather than placed on a guess. Each marker carries that opening message in its own
`extra.minds_evals.opening_message`. A case without steps has nothing to divide and gets no marker.

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
a run without it. `eval-config-time-to-mock.json` (three cases) measures how fast and how cheaply
the agent gets a styled mockup in front of the client: each case opens with a short, style-bearing
ask, and its goal-holding client stops the moment the agent says the mockup is ready to look at and
where to find it, never approving the design -- so the template's confirmation gate keeps the
hardening pass from starting. The client is told that an app it could open is enough: a trial's
workspace has no connected frontend, so the agent cannot open a tab for anyone, and a goal that
demanded one would hold it to something impossible. Its cases commission a `minds-app` (the mockup is served as a
registered app), a single-interaction UI flow whose screenshots the outcome judge reads against the
requested style, and a [`process`](#outcome-verification) block requiring the `build-app` and
`frontend-design` skills and forbidding any worker launch. `frontend-design` is a claude-code plugin
skill rather than one of the template's own, so that check measures the claude harness and fails on
pi-coding or codex whatever the agent did. Each case also carries a
[`timing`](#outcome-verification) block, which turns the latency into part of the score rather than
only an observation: the time from the opening ask to the reply that satisfied the client, scored on
a log-linear curve between per-case anchors, and zeroed if the `app` class failed. The anchors are
**provisional**, calibrated from a five-trial-per-cell A/B, and are meant to be re-set from the
first clean nightly. The raw numbers remain the per-turn `turns` records and
`conversation_seconds` in `state.json` (see below). All pin `mngr_branch: main`. A
config naming a branch that no longer exists fails at generation time, when the branch is resolved
to a SHA -- so a config pinned to a feature branch is worth keeping only while that branch is. A ref
given as a full SHA is taken at its word and costs no remote lookup, so a SHA that was never pushed
is caught later: for mngr by the shallow clone at generation time, and for the workspace template
not until the box clones it inside every trial.

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
- `state.json` carries `preparation_stage` as well: the last stage of workspace preparation the trial
  completed, one of `created`, `proxied`, `signed_in`, `chat_created`, `welcomed` and `switched`, then
  `conversation` once turn 1 has been sent. It is empty before the workspace exists. A trial with no
  proxy never records `proxied`, and one whose harness config names no model never records `switched`.
  A clone preparation or workspace create that raises still writes the state: the trial is marked
  `timed_out`, with `timed_out_reason` naming the error, before the error propagates to harbor.
- `state.json` carries three more readings of the run beside `entries`: `snapshot_byte_count`, the
  size of the last workspace snapshot pulled (0 when that pull failed, and `null` on a trial that
  pulled none, which is every trial with snapshots turned off); `decider_call_count`, how many calls
  the driver made to the decider model, which a goal entry pushes past the message count because the
  call that decides to stop is one too; and `client_messages`, every message the driver sent as the
  client, in order, blank ones left out.
- `elapsed_seconds` is the whole trial's, and `step_elapsed_seconds` is this step's -- the span
  `timeout_seconds` bounds, since for a stepped case that key is only the step's share of the
  conversation budget. On a flat case the two agree.
- `turns` in `state.json` records each *answered* client message as
  `{index, entry_index, exchange, sent_at, replied_at, reply_seconds, agent_message_count,
  message_count, tokens, cost_usd}`: when the message reached the workspace, when its reply was
  first seen complete, how long that took, how many agent messages the reply was made of, and what
  the agent spent answering it (`message_count` being how many of that turn's messages carried
  usage at all). `conversation_seconds` spans the first message to the last answered reply, which
  is narrower than `elapsed_seconds` -- that one also holds workspace creation, sign-in and the
  welcome turn -- and is `0.0` until a turn has been answered. Between them they are what a "how
  long, and how much, to the reply that presented a mock" question is read off. A turn earns a
  record only once its reply is in, the way an entry earns one only once it has stopped, so a
  trial that timed out waiting leaves no record for the message it died on and `waits_done` can
  exceed the records. The reply is noticed by polling, so `replied_at` is late by up to one poll
  interval (5 s by default) and `reply_seconds` overstates by the same: differences smaller than a
  poll mean nothing. Both keys accumulate across a stepped case's steps, the way `entries` does, so
  a later step's `conversation_seconds` starts at the case's first message and holds the verifier
  runs between the steps as well -- read a single step off the records' own `sent_at` and
  `replied_at`, not off that figure. Nothing at grade time reads any of this.
- Each entry's outcome is recorded in `state.json` under `entries`, as
  `{index, kind, exchange_count, outcome, detail, satisfied_at, satisfied_at_turn}` with `outcome`
  one of `completed`, `satisfied`,
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
  `satisfied_at` and `satisfied_at_turn` name the reply the client was satisfied by -- its UTC ISO
  timestamp and the 1-based index of the client message it answered -- and are empty (`""` and
  `null`) on every entry no client was satisfied by. The client rules on the conversation as it
  stands, so the reply is the last one answered when the entry ended, which is frequently **not** one
  of that entry's own exchanges: a goal already met by the previous entry's reply sends nothing and
  records `exchange_count: 0`. The [`timing`](#outcome-verification) class is what reads these; the
  outcome labels alone cannot say *when* the goal was met.
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
        "ui_flows": [{"name": "updated-content", "actions": "Open the roadmap.", "expect": "The new milestones are shown."}]
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

### Seeding a case

A case's first step can boot its workspace with a known app already in it, the way the workspace
boots with the template's own apps. The self-diagnostic fixture case
(`specs/minds-evals-self-diagnostics/spec.md`) uses this to measure the instrument against an app
whose every answer is fixed.

```json
"steps": [{
  "name": "instrument",
  "seed": {"app": {"source": "flow_lab_apps/todo", "name": "todo-fixture", "port": 8090}},
  "prompts": ["Reply with the single word: acknowledged. Do not do anything else."]
}]
```

- `seed` is a key of a stepped case's **first step** only. Harbor carries per-task files into the
  box through a step's `workdir/` alone, so a case that wants a seed and nothing else is a stepped
  case one step long. Generation rejects `seed` on a later step, on a flat case and at case level.
- `source` is a directory relative to the **minds_evals project root** (`apps/minds_evals/`), not to
  the eval config's directory as `files` sources are: a config under `configs/` could otherwise never
  reach `flow_lab_apps/`, and the flow lab and a trial must drive one copy of a fixture. The directory
  must hold an `app.toml` whose `name` is the seed's and whose `icon` names a file beside it; the
  template refuses to register a new app without an icon.
- `name` is the app's registry name and supervisord program name, and must pass the template's
  app-name rule (lowercase letters, digits and underscores in hyphen-separated runs, at most 32
  characters, no `host-` or `agent-` prefix, not `localhost` or `auth`).
- `port` is the loopback port the app binds, and may not be one the template's own services take
  (`7681`, `7682`, `8000`, `8010`, `8081`, `8300`, `8301`).

**How it travels.** Generation copies `source` into the step's `workdir/seed_app/`, and the step's
`setup.sh` moves it to `/work/step_seeds/<name>/`, beside the uploads and for the same reason. Nothing
goes into `environment/`, so the image cache is untouched.

**The build**, in the box, before the workspace exists, right after the eval-case commit:

1. **The seed commit.** A throwaway detached worktree of the case clone at `dwt_sha` gets the app at
   `system/fixtures/<name>/` and a `[program:<name>]` block appended to `system/supervisord.conf`,
   which registers the app from its manifest and serves the directory with
   `python3 -m http.server <port> --bind 127.0.0.1` (`autostart`, `autorestart`, `startretries=3`).
   It is committed with the eval-case commit's fixed identity and dates: `seed_commit_sha`, the seed as
   a change to the pinned template alone. Not under `system/apps/`, where the template's root
   `pyproject.toml` makes every directory a uv workspace member that needs a `pyproject.toml` of its own.
2. **The merge.** On the case clone's checked-out branch, `git merge --no-ff --no-edit
   <seed_commit_sha>` with the same identity and dates. A merge that leaves unmerged paths is a
   **conflict**: the paths are recorded and the merge aborted.
3. **The collision check.** The merged `system/supervisord.conf` is refused if it holds a second
   `[program:<name>]`, another program already registers the seed's name, or another program's
   `forward_port.py --url` claims the seed's port. Any of those is a **collision**, and the branch is
   reset to the case base.
4. **The seeded SHA.** On a clean build the branch head is the merge commit, `seeded_sha`, and the
   worktree is removed. The workspace is created from it, and `dwt_sha` still names the template the
   pair pins. Both commits have fixed dates, so rebuilding the seed on the same case base yields the
   same `seeded_sha`.
5. **The seed comes up.** Right after the workspace is created, before the proxy or sign-in, the
   driver polls `supervisorctl status <name>` and the app registry until the program is `RUNNING` and
   its row exists. A program supervisord has given up on (`FATAL`), or one still restarting
   (`BACKOFF`) after more polls than its start retries, ends the trial as timed out naming the seed,
   and so does a seed that is not up within the preparation budget.

**An impossible seed.** A conflict or a collision ends the trial before the workspace is created, so
nothing beyond the box is paid for. `state.json` records the trial timed out with a reason naming the
conflicted paths or the collisions, and its `seed` record carries `build_status` (`conflict` or
`collision`) and an `impossible_reason`; then the step raises `SeedBuildError`. Harbor records the
exception and aborts any later steps. A seeded trial must be judged from that record, never from its
reward.

**What gets recorded.** `state.json`'s `seed` holds the app name, `build_status` (`clean`, `conflict`,
`collision`), `seed_commit_sha`, `seeded_sha`, the conflicted paths, the collisions and the
`impossible_reason`; it is `null` on a trial with no seed build. `preparation_stage` gains
`seed_built` (reached before the workspace exists) and `seed_running`.

**What the bundle holds.** The deliverable bundle is cut from `seeded_sha`, so it is `seeded_sha..HEAD`:
the workspace's bootstrap commit and what the agent changed, and none of the seed. `repo_state.json`
records `seed_commit_sha` and `seeded_sha` beside `base_sha` (which on a seeded trial is
`seeded_sha`), and a replay rebuilds the seed onto the regenerated clone, checks it reproduces
`seeded_sha`, and unbundles onto it. An agent that commits nothing still leaves the bootstrap commit
in the range, so a bundle is written and `repo_state.json`'s `agent_commit_count` is `0`.

The seeded app's registry row is a class of its own, neither pre-existing nor delivered; see
[What counts as a delivered app](#what-counts-as-a-delivered-app).

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
  workdir/             only for a step with files or a seed
    step_files/<upload_id>/...
    seed_app/...       the seeded app, on a seeded case's first step
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
  running, an HTTP 200 from each delivered or seeded app's root path, and the delivered repo captured as a git
  bundle. "Delivered" is narrower than "not pre-existing" -- see below. Optional
  `min_registered_apps`, `http`, and `files` entries *refine* that set rather than replacing it.
  Unknown kinds and unknown keys are rejected at generation time.
- `test_commands` are run in the delivered repo and recorded for the judge, but never gated: gating
  them would punish cases whose prompts never mentioned tests.
- `ui_flows` are natural-language flows through the delivered UI, each with a verifiable end
  condition -- see [UI flows](#ui-flows). A flow may instead carry a `script` of exact actions that
  runs with no model call -- see [Scripted flows](#scripted-flows).
- `process` grades **how** the agent worked rather than what it delivered, for cases whose ask is
  about the route as much as the destination ("show me a mockup, and do not start
  hardening"). It takes `required_skills`, `forbidden_skills` and `max_worker_launches`, each
  optional, and each expanding into its own check:

  ```json
  "process": {
    "required_skills": ["build-app", "frontend-design"],
    "forbidden_skills": ["crystallize-creation", "update-creation", "heal-creation"],
    "max_worker_launches": 0
  }
  ```

  A required skill passes when the agent invoked it at least once, a forbidden one when it never
  did, and the cap when no more than that many background workers were launched. All three are read
  off the **chat agent's** own captured transcript, so they cost no extra probe -- and so a skill a
  background worker invokes satisfies no required check and trips no forbidden one. The cap counts
  the launches that transcript shows, one per worker name, which is what the worker capture starts
  from; the workers those workers launch in turn are captured but not counted. On a stepped case the
  transcript is the whole conversation's, so a later step's checks are answered by every step so
  far.
  On claude a skill is a `Skill` tool call naming it. The other harnesses have no such tool -- the
  agent reads the skill's body -- so a `skills/<name>/SKILL.md` path in a shell command, in a read
  tool's path argument, or in a codex code-mode program counts as the same invocation. Only what a
  call runs or opens is searched -- the command, the path, the program -- so a skill a claude call
  merely mentions, in the content it writes or in a delegation prompt, is not an invocation. A codex
  program is one argument and is searched whole, though, as is a heredoc body inside a command, so
  in those a mention reads as a reach. Neither is a call whose result came back an error: a
  `Skill` call answered `Unknown skill` -- an unresolved plugin, which `harness_quality` counts as a
  broken workspace -- never ran the skill, and a command that failed never opened the file. That
  veto is per call rather than per command, since the transcript's result names the call alone, so a
  call that ran several commands -- a compound shell line, a codex program -- loses every skill it
  named as soon as any part of it fails. A skill a plugin contributes is invoked under its qualified
  name (`frontend-design:frontend-design` is how the design skill the template enables through a
  claude plugin appears), so a bare name in the config also matches an invocation whose part after
  the last colon is that name, while a qualified name matches exactly -- and only a claude `Skill`
  call can ever carry one, since a skill file's path holds the bare name alone.
  Generation rejects a skill name in both lists, an empty `process` block, a negative cap, a name
  spelled as anything but a skill name (a bare `build-app` or a qualified `plugin:skill`, both
  accepted), and two names in one list that slugify alike (`build-app` and `build_app`), since the
  slug is the manifest entry's id and the second check would overwrite the first. If the
  transcript itself never came out of the workspace, or came out holding no record of the agent at
  all, every process entry is recorded `error` with reason `transcript_uncaptured` or
  `transcript_empty`: that is the instrument failing, not the agent, and it errors the trial rather
  than scoring it.
- `timing` grades **how long** the agent took, for cases whose ask is partly about latency ("show me
  a mockup" is worth much less an hour late). It measures one thing: the wall-clock from the case's
  first client message to the reply that satisfied a [goal entry](#eval-config)'s client.

  ```json
  "timing": {
    "fast_seconds": 150,
    "slow_seconds": 600,
    "requires_no_failures": ["app"]
  }
  ```

  Both anchors are required, positive numbers, and `fast_seconds` must be below `slow_seconds`.
  Unlike every other class the score is **continuous**, clamped and linear in log time:
  `score = clamp((ln slow - ln t) / (ln slow - ln fast), 0, 1)`, so `t <= fast_seconds` scores 1.0
  and `t >= slow_seconds` scores 0.0. The curve is scale-free, which is the point: the anchors are
  per case and the curve is global, so two trials that each hit their own case's `fast_seconds`
  score identically and the numbers stay comparable across cases. `requires_no_failures` lists
  expectation classes whose failure zeroes the time outright -- being fast at something other than
  what the case commissioned is not what the class measures. Each name must be a class the case
  actually declares (a class with no entries could never fail), never `timing` itself, and never
  `test_command`, which is recorded and never gated. An empty list is allowed and means the time
  stands on its own.

  The measurement is taken at trial time, from the conversation records the driver already keeps,
  and rides on the manifest entry as its `value`; grade time applies the curve to that number rather
  than recomputing it. The satisfying reply is the last one the client had seen when it declared
  itself satisfied, which is frequently **not** one of the goal entry's own exchanges: a goal the
  previous entry's reply already met sends nothing and records `exchange_count: 0`. Each entry in
  `state.json` therefore carries `satisfied_at` and `satisfied_at_turn`, naming the reply the client
  ruled on, empty on every entry no client was satisfied by.

  A client that was **never** satisfied is not an instrument failure: the agent never got the client
  to the mockup, which is unboundedly slow, so the class scores 0.0 and the trial still grades. That
  is why `timing` is the one scored class absent from `finalize.py`'s unmeasurable-class list. A flat
  case that declares `timing` but no goal entry is rejected at generation time, since nothing in it
  could ever declare itself satisfied.
- `fresh_env` is reserved and must be left unset. `fresh_env: true` is rejected at generation time,
  for the same reason: no fresh workspace is booted, so it would verify nothing.
- `max_judge_screenshots` bounds how many flow screenshots the outcome judge is shown in all, in place
  of the default 24; see [What the judges read](#what-the-judges-read).
- A `ui_flows` entry may carry notes for its reader under keys starting with `_comment`, since JSON has
  no comments. Generation ignores them there, and nowhere else accepts them.

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
  supervisord.conf       # verbatim capture of the merged supervisord config the rows are joined through
  isolated_instance_services.txt  # verbatim capture of the isolated-instance state the preview rows are read from
  repo_state.json        # HEAD sha, the base and dwt-tip shas, commit counts (all, bootstrap, agent), git status --porcelain
  deliverable.bundle     # incremental `git bundle <clone HEAD>..HEAD` -- the workspace's bootstrap commit, then the agent's own
  common_transcript.jsonl    # the workspace agent's common transcript, as `mngr transcript --format jsonl` wrote it
  workspace_trajectory.json  # the unmodified ATIF document `mngr transcript --format atif` built from it
  tickets.jsonl          # one {id, type, status, is_step, agent, title, summary, created, closed} per data/.tickets/*.md
  workers/agents.json        # `mngr list --format json` at collection time, on every trial, labels and errors included
  workers/listing.json       # how that listing itself went: {exit_code, errors, is_complete}
  workers/captures.json      # one record per worker the capture handled, overflowed launches included
  workers/<name>/            # per launched worker: trajectory.json, common_transcript.jsonl, reports/
  http/<check>_<n>_<app>.json  # per probe: status, headers, timing, body head (256 KB cap)
  flows/<slug>/log.jsonl # per UI-flow step: the verbatim page state, the action, the reasoning, the frame's size
  flows/<slug>/run.json  # how that flow ended: {status, reason, verifier_call_count}
  flows/<slug>/step_NNN.png  # a screenshot per step
  trace.jsonl            # every bridge command the collector ran, failures included
```

Three manifest entry kinds carry a structured reading beside their prose. An HTTP probe's entry
carries `status_code`, the status it observed (`0` for a refused connection, null when the bridge
never delivered the probe). A `test_command` entry
carries `exit_code`, and each declared files check has an entry of its own (`files_<n>`) carrying
`matched_count`: how many inventory paths its glob matched. The count is taken inside the workspace
during the inventory walk, over exactly the entries the inventory records and with the same
`fnmatchcase` matching the verifier applies. The entry is `failed` below the check's `min_count`, and
`error` when the inventory itself was not captured. The verifier still scores `files_expectations_met`
from the inventory file, so a corrected glob is picked up on a regrade.

`tickets.jsonl` holds the workspace's `tk` records: every ticket file under the repo's `data/.tickets/`
except its README, read in one exec and bounded in size. `is_step` is the ticket's `step: true`, and
`summary` is the `## Summary` section tk writes when a ticket is closed. Like the transcript, it is the
trial's record rather than outcome evidence, so it adds no manifest entry. A capture that fails writes
the file with one `{"failure_reason": "<why>"}` record and nothing else, which is how an unread
directory is told apart from one holding no tickets. A ticket file with no frontmatter is left out.

The agent listing is taken on every trial with a workspace, not only when the chat agent's commands
show a worker launch. `workers/agents.json` is written from the listing itself, so it is there even
when no worker was captured. It keeps each agent's mngr `labels` (`agent_created` marks a worker
another agent created) and the listing's `errors`, which say whether the listing reached every
provider. `workers/listing.json` beside it records how the listing command itself went -- its
`exit_code` (`null` when the bridge never ran it), those same `errors`, and `is_complete`, true only
for a listing that ran, exited 0 and reported no errors. Only a complete listing can call a worker
it does not name destroyed, so an unread listing and an empty one must not read alike.

`workers/captures.json` records every worker the capture handled: one object per capture with the
worker's `name`, `agent_id`, `agent_type`, `state`, `depth`, `lead_name`, its `directory` under the
bundle and whether each of its three parts came out, then one per launch the caps left uncaptured,
carrying `is_overflow: true` and nulls for everything a capture would have answered. It is written
on every trial with a workspace, the empty list included.

Each `init` and `action` record in a flow's `log.jsonl` carries `screenshot_byte_count` and
`is_screenshot_png`. The step script reads both back from the frame it wrote, so a frame that exists
but is empty, or is not an image, shows as such without being opened. Each finished flow also writes
`flows/<slug>/run.json`: the flow's `status`, the `reason` it did not complete (empty when it did),
and `verifier_call_count`, the calls that flow alone made to its verification agent (zero for a
scripted flow, which makes none). It is a file of its own rather than a last line of the log because
the judge's renderer reads every log record as one flow step.

Every manifest entry carries a status where **`failed` means the workspace fell short and `error`
means the harness could not find out** (the bridge died, a probe timed out). That distinction is
load-bearing in both directions. `error` entries are excluded from the criteria they would have fed,
so an agent is never charged for a broken instrument, and a wholly unmeasurable `files`, `app`,
`http`, or `process` class errors the trial rather than scoring it (see
[Error versus zero](#error-versus-zero)). But a workspace whose app registry exists and
lists nothing is the agent shipping nothing, which scores as `failed` -- not waved off as evidence
the harness could not gather.

The registry, service, and inventory capture runs for *every* trial that got as far as a workspace,
including cases with no expectations, which is what makes a ships-nothing trial diagnosable. The
manifest's `is_registry_present` says whether `data/.state/apps.toml` could be read at all, since the
verbatim `apps.toml` capture is empty both for a registry that could not be read and for a workspace
that registered nothing. The
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

The bundle is never only the agent's work. The workspace template's first boot commits the tree the
workspace was created with, as `Initial workspace commit` authored `minds-bootstrap
<bootstrap@minds.local>`, dated at boot and made with `--allow-empty`, so it sits on the base before
any agent runs. The bundle keeps it, because the agent's commits sit on top of it and a replay
unbundles onto the regenerated base. `repo_state.json` counts it apart: `bootstrap_commit_count` is
`1` when the first commit on HEAD's first-parent history beyond the workspace's starting commit (the
seeded commit on a seeded trial, the base otherwise) has that commit as its parent and the
bootstrap's author and subject, and `agent_commit_count` is every commit beyond the starting commit
but that one. The author alone does not identify it: the bootstrap also sets that identity as the
repo's own when it has none, so a commit made without an exported author carries it too.

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
  the names the workspace's own supervisord config registers through its `forward_port.py`
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

A **seeded row** is a third class beside pre-existing and delivered: the app a case's `seed` put in
the workspace (see [Seeding a case](#seeding-a-case)). It is in the registry and in
`system/supervisord.conf` from boot, so the pre-turn-1 measurement names it too; the case declares
it, and a name that is both is seeded, never pre-existing. The HTTP probes (the `registered-apps`
fan-out and a probe naming the app) and the UI flows are aimed at delivered and seeded rows alike,
while `min_registered_apps` and the service checks count delivered rows only. A seeded trial where
the agent built nothing therefore delivered nothing, and a case that expects exactly that declares
`min_registered_apps: 0`. The manifest records the seeded names as `seeded_registrations`, empty on
an unseeded trial.

If the registry cannot be read the pre-existing set is **unknown**, not empty -- otherwise every app
the workspace booted with would count as delivered. The app, HTTP, and UI-flow entries are then
recorded `error` with reason `preexisting_unknown`, so the trial is unmeasured rather than scored
wrong, and `preexisting_registrations` is `null` (a different claim from a workspace that served
nothing). A seeded trial is no exception: its seeded row is not probed on a registry nobody could
read. The registry and service capture still happens either way.

A registry name is not a supervisord program name -- a multi-port app registers extra origin rows
(`<name>-admin`) that no program owns. The service-health check joins a row to its program through
the `forward_port.py` invocations inside each `[program:*]` block of the workspace's supervisord
config, and falls back to a program named exactly like the row, which covers a service that
registers its port at runtime instead of from the config. A row with neither is recorded as
`no_supervised_program`: the app was started by hand and would not survive a restart.

That config is `system/supervisord.conf` plus every `system/supervisord.conf.d/*.conf` beside it:
the default template declares each program in its own drop-in there, and its layout test pins
that directory as the one its `[include]` glob names, so the capture reads the directory by name
rather than parsing the glob. Reading the main file alone finds no `[program:*]` at all, so the
join is empty. The service-health check mostly survives that on its same-name fallback, but a multi-port
app's extra origin rows (`<name>-admin`) own no program of their own and are recorded
`no_supervised_program`. The costlier half is `resolve_preexisting_registrations`, which has no
fallback: a template app whose service had not registered its port when the snapshot was taken
then appears in neither half of the pre-existing set, so it is scored as something the agent
delivered. An app-free workspace gives the same empty answer, so neither shortfall could be
told from a true negative -- which is what the capture checks for separately. supervisord runs
what its config declares, so a `supervisorctl status` listing that names programs while the
captured config declares no `[program:*]` or `[eventlistener:*]` section at all is a read that
missed part of the config. The pre-existing set is then **unknown** rather than one that
silently omits a template app, and a row whose owning program could not be resolved is recorded
`error` with reason `supervisord_conf_unreadable` rather than `no_supervised_program` -- an
unmeasured row instead of one scored against the agent. The check keys on declared sections
rather than on `forward_port.py` calls, so a template whose apps all register their ports at
runtime, and whose config therefore registers nothing, is not mistaken for a broken read.

## UI flows

Liveness probes cannot see whether the app does what was asked -- a 200 with a stack-trace page
passes one. A `ui_flows` entry is a natural-language walk through the delivered UI with a verifiable
end condition, and it is the only level that checks the actual promise in the prompt:

```json
"ui_flows": [
  {
    "name": "persistence",
    "actions": "Open the app. Add a task named 'persist me'. Reload the page.",
    "expect": "'persist me' is still visible after the reload."
  }
]
```

A flow's `name` names its evidence directory, slugified, and must be unique within a case *after*
slugifying -- so `Add Task` and `add_task` collide and are rejected at generation time. A flow may
also carry a `surface`. `origin` is the default and the only implemented one; the reserved
`minds-ui`, which would drive the Minds chrome and reach the app as an embedded iframe, is rejected
at generation time rather than silently falling back.

A flow may carry a `start_path`, where on the app's origin it opens: empty (the default) opens the
root, `?latency=300` opens the root with that query, and `/tasks` opens that path. Each flow carries
its own, so one case can drive an app under several of its query-string behaviours. It must be empty
or begin with `/` or `?` but not `//`, and hold only printable ASCII with no spaces or backslashes;
anything else is rejected at generation time, and again when the driver reads the case back. The
rule is what keeps a flow on the app it grades: `//host` and `http:...` name another origin outright,
and a browser reads a backslash as a slash and drops tabs and newlines inside a URL, so either can
turn an innocent-looking path into `//host`.

**The executor drives the app's forwarded origin from inside the box.** Flows run at the end of the
collection phase, inside its budget, in a headless Chromium the box launches for the flow -- its own
profile and its own CDP port, so no flow inherits another's cookies or storage -- navigating to the
flow's start path on `https://<label>.agent-<hex>.localhost:8431/` -- the app's own label on the
workspace's agent-keyed origin -- served by a `mngr forward` instance the driver owns. A host-side verification agent (the
decider's sibling) reads the page, decides one action, and a box-side step script performs it,
screenshots the result and reads the page back, all in a single box-local exec.
The reasoning stays host-side, so the loop is budgeted, logged, and attributable to harness spend.

This tests the app **through** the product's serving path -- forward proxy, SSH tunnel, label
origin, session cookie -- rather than under it. The browser is armed before its first navigation
with the trial's pre-auth cookie, scoped the way the proxy scopes its own: to the workspace's whole
origin family, so a flow stays authenticated wherever under it the app sends the browser. Elements
are addressed by ARIA role and accessible name, taken from Playwright's `aria_snapshot`, which is
also what the flow log records verbatim for the judge. An element the tree lists with no name at all
(a checkbox with no label, say) is addressed by the ref the snapshot prints for it (`[ref=e9]`); the
step script checks the ref against a fresh snapshot of the page before acting on it and refuses one
that no longer sits on the role the agent read, so a page that changed in between costs the flow one
recorded step rather than a click on whatever inherited the number. Such a step carries the ref on
its `log.jsonl` line as `target_ref`, and the judge's digest marks it and says what the mark means: a
control with no accessible name is an accessibility defect of the delivered app, recorded for a
measure of its own and, unless the declared actions or the `expect` call for accessibility, not
counted against the flow.

A decision the agent makes that cannot be acted on -- an action that does not exist, an element
addressed by neither name nor ref -- ends the flow as an instrument error, with the decision's own
words in the driver log, in the manifest entry's detail, and on a `(no usable action)` step in the
flow log that shows the page the decision was made on.

The verification agent's spend is reported as `metadata.verifier_agent_usage`, beside
`decider_usage` and never folded into the agent's own cost fields. It runs on the decider's model by
default; `--ak verifier_model=...` overrides it.

**Trial time records completion, never achievement.** A flow whose declared actions the agent
carried out is `passed` whatever the page showed; nothing here evaluates the `expect`. A step that
fails mid-flow -- an element that is not there, a click that hit nothing -- is recorded on that step
and the flow carries on, because the page below shows the truth and the grade-time judge reads it.

**A step waits for the page to react before it reads it.** After a click, a key press, typing or a
scroll, the step script watches the DOM through a MutationObserver installed in an isolated world
of its own (the way Playwright's own instrumentation runs: it shares the document but none of the
page's JavaScript, so the app cannot see it), waits for the first mutation and then for the DOM to
go quiet, and only then captures the ARIA tree and the screenshot. A render the app defers -- a
framework batching updates -- is therefore captured by the step that caused it. A click or key
press waits up to 3 s for the page to start reacting; typing and scrolling, which oblige the app to
nothing, up to 1 s; a page that has started reacting gets 5 s to go quiet. What the watch saw is
the step's `reaction` (`settled`, `none`, `still_changing`, or `unobserved` for the steps that do
not watch: an `open` or a `reload`, which wait on the network instead, and an action that failed),
recorded on the `log.jsonl` line beside the prose the agent reads. That prose tells a dead control
(`nothing happened`) from one that acknowledged the gesture without a visible result (`the page
reacted but shows nothing new`: a highlight, an armed state), which call for opposite next moves.
A scroll is the one action whose own effect neither signal can show, so it reports `nothing
happened` even when it worked; the agent's rules exempt it from the rule against repeating an
action, since repeating a scroll is how it reaches further down a page.

**The agent waits rather than reloads.** A `wait` action performs nothing and gives the page up to
10 s to change, for a pending state such as a spinner or a "saving" notice. The agent reloads only
where the declared actions say to; a reload the flow did not ask for is its last resort for an app
that has stopped responding, and the judge's flow digest reads it as evidence against the app,
since an app that needs a full reload to show its own state has already failed.

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

### Scripted flows

A flow can instead carry a `script`: the actions to perform, in order, in the executor's own
vocabulary. A scripted flow has a `script` and an `expect`, and never `actions`:

```json
{
  "name": "add",
  "script": [
    {"kind": "input", "role": "textbox", "target": "New task", "text": "walk dog"},
    {"kind": "click", "role": "button", "target": "Add"},
    {"kind": "reload"}
  ],
  "expect": "'walk dog' is still listed after the reload."
}
```

Each action is a `kind` plus the fields that kind needs, and no others; every other field is left
out (or empty, or zero):

| kind | needs | does |
|---|---|---|
| `click` | `role`, and `target` or `beside` | clicks the element with that ARIA role and accessible name, or the nameless one `beside` locates |
| `input` | `role`, `target` or `beside`, and `text` | types `text` into that element |
| `keys` | `text` | presses a key combination, e.g. `Enter` |
| `scroll` | `amount` | scrolls by that many pixels; negative scrolls up |
| `open` | `text` | navigates to `text`, a start path on the app's origin under the `start_path` rule |
| `reload` | nothing | reloads the page, keeping the session |
| `wait` | nothing | performs nothing and gives the page up to 10 s to change |

`done` is not written: every script ends with one. Because that `done` takes a step of the flow's
15-step budget too, a script holds at most 14 actions. An unknown kind or key, a missing or unused
field, and an `open` that could leave the app's origin are all rejected at generation time, and
again when the driver reads the case back.

An element the page gives no accessible name -- a checkbox with no label, say -- has no name to
write, and its snapshot ref cannot be written either, because refs are numbered when the page is
read. A script locates such an element instead. `beside` names text, and the action addresses the
element of its `role` that has no name and whose parent element holds a line containing that text.
The text is looked for with the recorder's `[ref=...]`, `[active]` and `[cursor=...]` suffixes
dropped, and the element's own line never counts:

```json
{"kind": "click", "role": "checkbox", "beside": "walk dog"}
```

The locator is resolved against the page state the step is decided on, and the step is performed
by the ref it picks out, so it is checked against a fresh snapshot and recorded with `target_ref`
like any step addressed by ref. A locator that picks out no element, or more than one, is a decision
that cannot be acted on: the flow ends as `verifier_agent_failed`, and its `(no usable action)` step
and its manifest entry say what the locator found. Such a miss is always an eval error, never the
app's failure; no per-action setting makes a script count it against the app. `beside` together with
`target`, or on an action that addresses no element, is rejected with the other rules.

A scripted flow makes no model call. Its actions are handed to the executor exactly as written,
each recorded with the reasoning `scripted`, followed by the closing `done` and the fixed reading
`scripted flow; no reading`. The same defect therefore shows as the same reading on the same step
every run, and the flow lab can confirm it locally before a trial runs it. An action naming an
element the page does not have is recorded on its step and the flow carries on, as for any flow.

Generation renders the script into the flow's `actions` as numbered prose ("Step 1: type 'walk dog'
into the textbox named 'New task'. Step 2: ..."), whose numbers are the log's step indices, so the
judge reads a scripted flow the way it reads any other. The verification agent drives only the
flows without a script: a trial with no key for it records those as `verifier_agent_failed` and
still runs its scripted ones, which add nothing to `verifier_agent_usage` either.

### The flow lab

A flow can be driven against a **local app, with no box, no workspace and no proxy**: the same
step script the box uploads and runs, exec'd one process per step under this project's own
interpreter, against a headless Chromium launched here with the box's flags, on a static app served
from a directory on a local port. What the lab drops is the box transport and the proxy in front of
the app; everything below the loop is the code a trial runs, so an executor change measured here is
measured on the instrument. It needs playwright's Chromium once:

```bash
cd apps/minds_evals && uv run python -m playwright install chromium
```

`flow_lab_apps/` holds the apps the lab's tests drive. `todo/` is a to-do list whose query string
dials in the page behaviours a real app can have: `?latency=<ms>` applies every change that many
milliseconds after the action (the shape of any framework that batches updates), `?pending=<ms>`
answers every change with a "Saving..." status first and applies it that many milliseconds later
(the shape of an app talking to a backend), `?arm_delete=1` makes delete a two-click control whose
first click is acknowledged by a highlight and nothing else, `?dedupe=ci` drops a case-insensitive
duplicate without saying so, `?ticker=1` keeps a clock repainting so the DOM never goes quiet, `?jank=<ms>` holds the page's main
thread busy for that long once a second (what a loaded machine does to a descheduled renderer), and
`?unnamed=1` gives each task's checkbox no label association, so the tree lists it with no name and
the ref is the only handle the page offers.
Its "Start over" link is a real navigation, for the step that has to survive one -- immediately, or,
under `?pending=<ms>`, only after the click has been acknowledged, which is the redirect that lands
while the step is still watching the page it is about to lose. `test_flow_lab.py` pins what the
step script, the flow loop and the state summariser report for each of those, driven by scripted
flows; a test that documents a defect the executor still has is an expected failure naming the
issue, and stays one until the executor catches up. An app taken out of a trial -- the
deliverable bundle under `verification/`, checked out into a directory -- is driven the same way,
which is how a flow that went wrong in a trial becomes a fixture.

The real verification agent is run against a local app with:

```bash
uv run --project apps/minds_evals minds-evals flow-lab \
  --app apps/minds_evals/flow_lab_apps/todo --start-path '?latency=300' \
  --actions "Add a task named 'walk dog'. Mark it complete. Delete 'walk dog'." \
  --expect "'walk dog' is gone" --output /tmp/flow-lab/todo
```

It prints each step as the agent's history would carry it, reports the agent's spend, and leaves
`log.jsonl` and the step frames in `--output`, shaped as a trial's flow directory. `--start-path`
takes a case flow's `start_path` under the same rule and joins the served origin the same way, so a
flow from a case is reproduced here with the value it declares. `--model`
selects the agent's model (the decider's default otherwise), and `ANTHROPIC_API_KEY` is required.
The exit code says whether the flow completed its declared actions; whether the `expect` holds is not
decided here, exactly as at trial time.

A scripted flow is run the same way with `--script` in place of `--actions`, naming a JSON file
that holds the flow's `script` list as a case would declare it, under the same rules. It needs no
key and makes no model call:

```bash
uv run --project apps/minds_evals minds-evals flow-lab \
  --app apps/minds_evals/flow_lab_apps/todo --start-path '?arm_delete=1' \
  --script /tmp/flow-lab/arm-delete.json \
  --expect "'Buy milk' is gone" --output /tmp/flow-lab/arm-delete
```

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
  counts as a failure and why the dimension is separate. Every criterion in every dimension is
  framed so a higher number is a better outcome. It is **scored on the claude harness only**. The
  report the judges score is built by claude-shaped rules -- a skill invocation is the `Skill` tool,
  and two of the six signatures name claude's own skill and plugin vocabulary -- so on a pi-coding
  trajectory it comes out thin because those rules found nothing, not because the harness held, and
  the judge is told that a report with nothing in it is a 5: a false pass rather than a measurement.
  (The shell-level signatures are harness-blind in what they match, so they still fire on a
  pi-coding or a codex trajectory, which is why the reports themselves are written whatever ran. On
  codex the scanned shell output is the only way in: mngr's codex converter records every tool
  result with `is_error` false, a failed code-mode program included.) The verifier reads the harness
  off the graded trajectory: a captured document's own `agent.name`, and otherwise -- the driver's
  hand-built fallback names the *driver* there, and mngr writes `unknown` when it cannot resolve the
  agent -- the `harness` the [arm block](#what-the-arm-records) recorded. A trajectory that says
  neither is graded as claude, so a trial that merely lost its transcript keeps every dimension it
  always had. On any other harness the judge is not called and the dimension is left out of the
  trial's scores. Every trial's `reward-details.json` carries a `harness` block naming what ran and
  whether the dimension applied, so a dimension missing from the scores reads as one that does not
  apply here rather than one that failed to emit. The judges still see the conversation itself: what
  they grade it on is the agent's own messages, which carry no harness's vocabulary. The one place
  the judged transcript does read a harness's tool names is its executing-tool scan, which lists
  each harness's spelling of the shell (`Bash` and `BashOutput` on claude, `bash` on pi-coding, and
  on codex the code-mode `exec` tool plus the `wait` that returns a still-running program's output)
  so that what a command printed reaches the progress timeline whatever ran it. codex runs its shell
  from inside a JavaScript program, so the command text that `progress_timeline_was_read`, the
  `S1=$(tk create --step ...)` title fallback and worker discovery look for is a string literal
  inside that program: the verifier and the host-side worker scan read it out of every
  `tools.exec_command(...)` and `tools.shell_command(...)` call, in any of JavaScript's three string
  forms. A command the program assembles from variables at run time is not a literal and cannot be
  recovered that way; a template literal is read with its `${...}` placeholders left in.
- **`outcome`** (expectation cases only; the generator omits the verifier's `outcome/` directory
  otherwise, so rewardkit never emits a partial score for it) -- one criterion per declared check
  class (`app_registered`, `http_expectations_met`, `files_expectations_met`, `ui_flows_completed`,
  `process_expectations_met`, `time_to_goal_within_expectations`) plus a `works_as_expected` likert judge over the rendered
  expectations, the manifest, the conversation, and the flow evidence. The conversation is in there
  deliberately:
  `DECIDE_FROM_PERSONA` turns and goal entries are both free-form -- and a goal entry is a whole
  stretch of negotiation, not one line -- so a client who steers the build mid-conversation must be
  graded against the evolved ask.

`ui_flows_completed` scores COMPLETION: the fraction of measurable flows that carried out their
declared actions. It does not score whether the app did what a flow's `expect` describes. That is the
judge's ruling, made from the step log and the screenshots. Trial time collects; grade time
verifies.

Every criterion but one is a fraction of pass/fail checks. `time_to_goal_within_expectations` scores
a single continuous measurement -- the seconds to the client's goal -- on the clamped log-linear
curve between the case's own two anchors; see [`timing`](#outcome-verification). It carries no more
weight than any other criterion in the dimension: rewardkit averages every `.py` criterion into one
programmatic reward, so declaring `timing` on a case makes latency one voice among its checks rather
than a gate on them.

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
declare `ui_flows` or a `process` block -- which register `ui_flows_completed` and
`process_expectations_met` on their own -- their outcome dimension is the judge alone rather than an
even split with the checks. Those scores are on the same 0-1 scale but are not the same measurement;
see [Outcome verification](#outcome-verification).

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
  digest carries, per flow: the declared actions, the `expect` the judge is to rule on, the completion
  status, the agent's own description of the final page (evidence, not a verdict), then every step's
  action, reasoning and page state. `judge_screenshots/` holds each flow's last four frames, up to
  24 in all, each under rewardkit's 1 MiB judge limit. Over that bound the earliest flows' frames are
  dropped first, so a case whose flows ask for more sets its own bound as
  `expectations.max_judge_screenshots`, an integer from 1 to 100: the screenshots are the judge
  request's only images, and the Anthropic API refuses a request with more than 100. Both are written
  unconditionally: rewardkit renders a listed path it cannot find as a visible `[not found]` block,
  while an empty listed directory renders *nothing at all*, which is why the digest states the
  screenshot count instead of leaving the judge to infer it.
- `harness_main.txt`, `harness_workers.txt` and `harness_failures.json` (from `trajectory.json` and
  each `verification/workers/<name>/trajectory.json`): the processed harness reports the
  `harness_quality` judges read, and the per-scope signature counts its programmatic criteria score.
  A raw trajectory runs to hundreds of KB and is mostly ordinary work, so each report keeps only the
  steps that bear on whether the harness held. `render_harness_report.py` holds the signature list
  and the rules for what is kept and what is counted.

These inputs are derived inside the verifier container, under `/logs/agent`, which the trial's job
directory never receives. So on its way out, whatever happened before, `test.sh` copies
`judge_transcript.txt`, `progress_summary.json`, `harness_failures.json` and
`judge_flows_digest.txt` into
`/logs/verifier/derived/`, which harbor collects into the trial's `verifier/derived/`. It adds
`judge_screenshots.txt` there too, naming the screenshots the judge was given. An input a failed
pre-step never wrote is skipped, and a copy that fails never changes the grade
(`keep_derived_outputs.py`).

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
  - a declared `files`, `app`, `http`, or `process` check class whose every recorded entry is an
    `error`. UI flows are exempt: every flow erroring usually means only that the executor was
    unavailable, and voiding the trial would discard its conversation-quality measurement over that.
    `timing` is exempt for the opposite reason: it never records an `error` at all. Its one
    unmeasurable state -- a client that never said its goal was met -- is the agent taking
    unboundedly long, which is a measurement and scores 0.0.

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

**`per_turn` breaks the same file down by client message.** Each answered turn carries its token
buckets and its cost beside the times it took (the same records `state.json` publishes as `turns`;
see [Eval config](#eval-config)), so "what did the reply that presented a mock cost" is answerable
without re-deriving anything from the transcript. These are always the *transcript's* account,
whatever sourced the totals above them: the proxy log records requests with no way back to the
message that provoked them, so it cannot be split per turn. So a per-turn figure carries the
transcript's caveats -- no delegated or worker spend, everything priced at the standard rate
whatever tier served it, and `cost_usd: null` on a codex turn, whose stream reports no usage at all.
The per-turn figures are a floor on the trial's spend rather than a partition of it, so they do not
sum to the totals above: the welcome turn happens before the first record, a record is taken as soon
as the agent reports WAITING so the workspace's turn-end flow can spend past it, and a proxy-metered
trial's totals come from another source entirely.

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

The log also records every request the proxy failed, as a record with `"outcome": "failed"`, the
model, `status_code` (null when litellm reported none) and `error_class`, and zero in every token
bucket. A successful request's record carries `"outcome": "succeeded"`, and a record with no `outcome`
at all is read as a success. Failures add no tokens, no cost and no per-model row to the account;
they are counted as `failed_request_count` beside it, in `usage.json` and in `workspace_usage`. A
stream that breaks after billing some tokens is recorded as a failure with zero tokens, so that
partial spend is not in the total.

**The proxy meters the `anthropic` lane only.** Its address reaches the workspace through the
`ANTHROPIC_BASE_URL` line of the claude sign-in and nowhere else, and its model list is
Anthropic-only, so on any other [lane](#harness-and-model-arms) `--ak proxy=true` is refused at
construction rather than starting a proxy nothing would call. Non-Anthropic trials are priced from
the transcript, which is the path above: their `is_speed_observed` stays `false` and their
`is_cost_complete` follows the transcript rules.

**A codex trial's cost is unknown, because there is nothing to price.** The workspace reports no
token count for a codex turn: its chat app stamps `usage` on a claude message and leaves it null on
a codex one, and this driver skips a message that reports no usage rather than counting it as zero.
So the workspace agent's every token bucket in `usage.json` is `0` while its `cost_usd` is `null` --
unknown, not free, which is the distinction that keeps a codex arm out of a cost comparison instead
of dragging its average down. `is_cost_complete` is not the field that says so: it asks only whether
delegated and worker traffic is accounted for, so it stays `true` on a codex trial. `cost_usd` being
`null` is the whole signal, and a filter that reads `is_cost_complete` alone takes a codex trial for
a complete measurement. Nothing in the trial is wrong, and the proxy that would meter the lane
instead is refused there (see above). Read a codex trial for its conversation and its outcome
scores, never for its spend. The decider's and the verification agent's own figures are unaffected:
they call the Anthropic API directly and are metered by that client.

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
was requested, `confirmed` or `unconfirmed` -- or `not observable` on a lane whose harness names no
model on a transcript step, where nothing could ever confirm one: `anthropic haiku confirmed`,
`api-key anthropic/claude-haiku-4-5 unconfirmed`, `openai gpt-5.6-sol not observable`,
`anthropic default`. `--summary-json <path>` writes
the same rows as JSON with `is_passed`, `job_name`, `modal_environment_names` (exactly the list the
cleanup below acts on), and the harness half as its own fields (`lane`, `requested_model`,
`is_model_confirmed`, `wrong_model_reason`). An oracle run
(`harbor run -a oracle`) is checkable the same way: it writes a `state.json` and a reward, and its
fabricated evidence carries no `error` entries.

### Checking a diagnostic run

`minds-evals check-diagnostics <job_dir> (--expected <table> | --record-only) [--summary-md <path>]
[--summary-json <path>]` reads a self-diagnostic job (see
`specs/minds-evals-self-diagnostics/spec.md`) and gives each of its trials one verdict. Exactly one
of `--expected` and `--record-only` is given: the first grades the job against a family's
expected-facts table, the second computes the same facts and asserts nothing.

**The facts are computed at check time**, by `evidence_facts.py`, from the records the job directory
holds -- the same files the graders read. Nothing in the driver computes or stores one, no
`state.json` carries a fact block, and no verifier reads one, so a fact can only be as right as the
record under it. Every step of every trial, flat or stepped (`steps/<name>/`), yields one block: a
flat mapping from a dotted fact name to a value.

A fact reports one of three things, and a table tells them apart:

- **omitted** -- the record was never due on that step: the case declared no such check, the trial
  pulled no snapshot, the step is the first so there is no earlier one to be step-local against. The
  case config in the step's `instruction.md` is what decides what was due, and an omitted fact is
  absent from the block rather than carrying a value.
- **not recorded** -- the record was due on a step that ran and is absent or unreadable. That is the
  instrument failing to write its record, so it fails any fact a table asserts on it, whatever the
  table expected. `case.readable` and `manifest.readable` are the two facts that are never "not
  recorded": they exist to say whether the step's own case declaration and its evidence manifest --
  the records that say what every other fact was due -- were there and well-formed.
- **null** -- the record is there and cannot answer: a registry the collector could not resolve, a
  ticket capture that reports a failure, a listing too incomplete to speak for an absent worker. A
  recorded null matches only `expected: null`.

**The table** is JSON, and a key it does not know is refused:

```json
{
  "case_id": "fixture",
  "requires": ["prep.stage_reached"],
  "facts": {
    "prep.stage_reached": {"expected": "conversation"},
    "manifest.readable": {"expected": true},
    "listing.complete": {"expected": true, "health": true},
    "workers.no_phantoms": {"expected": true, "requires": ["listing.complete"]},
    "gates.held@work": {"expected": true},
    "snapshot.bytes": {"at_least": 1000000},
    "transcript.agent_steps_with_model_name": {
      "expected": "all",
      "by_harness": {"codex": {"expected": "none", "known_failure": {"issue": 898}}}
    }
  }
}
```

- **Fact references.** A key names the fact and the step it is read from: `<fact>` reads the last
  step that ran, and `<fact>@<step>` pins one (`gates.held@work`), so the same fact can be asserted
  on several steps. A step that never ran has no block, so every fact pinned to it is not recorded.
- **`case_id`** names the case the table grades: `check-diagnostics` refuses a job whose trials ran
  another case, with an error naming both, instead of grading them. A table that names none applies
  to any case, which is what the live-invariants table below does.
- **Matchers**: every entry gives exactly one of `expected` (JSON equality, where a boolean never
  equals a number, and `null` matches only a recorded `null`), `at_least` and `at_most` (a recorded
  number within the bound), and `contains` (a member of a recorded list, or a substring of a
  recorded string).
- **`by_harness`** overrides a fact for the harness the trial's `arm.harness_config` records. An
  override states a matcher, a `known_failure`, or both; what it leaves out stays the fact's own.
- **`known_failure`** (`{"issue": N}`, or `{"reason": "..."}` for a defect no issue tracks) says the
  value the fact's matcher states is the defect's rather than the healthy one, the way the codex
  override above states the `none` codex records until #898 is fixed. A trial that records it is
  `known`. Every known failure is strict, so a trial that records anything else is a **failure**
  saying the defect changed or was fixed and the mark must come off. An entry whose matcher is
  `expected: null` must carry a mark: a fact declared unobservable is a defect with a name, never a
  permanent exemption.
- **`compliance: true`** marks a fact that says whether the agent did what the prompt asked;
  **`health: true`** marks one that says whether a compliance source could be read at all. A fact is
  one or the other, never both, and neither may declare a known failure. A compliance fact that
  reads false is *not followed* and a health fact that misses is a failure; either that reads null
  or was not recorded is a **failure**, since the instrument could not read it, which is never the
  agent's doing.
- **`requires`** on a fact names compliance and health facts of the same table only: when one of
  them misses, the fact is reported as a *precondition not met* rather than asserted. The table's
  own top-level `requires` may name any fact the table asserts and applies to every fact at once, so
  a trial that never got through preparation reads as that one miss rather than as every fact
  failing. A requirement is met only when its fact passed.
- Validation refuses an unknown key, more than one matcher, a bound of `null`, a reference that is
  neither `<fact>` nor `<fact>@<step>`, a requirement that is not a compliance or health fact, a
  requirement cycle, a table-level requirement the table does not assert, a fact that is both
  compliance and health, and a compliance or health fact that declares a known failure.

**Verdicts**, in precedence order:

- **not measured**: no step's `state.json` records a `preparation_stage` from `created` on and no
  seed-build failure is recorded, which includes a trial harbor stopped (an environment exception,
  say) before the driver wrote any state. Nothing is asserted.
- **failed**: an instrument fact missed, a known failure's defect was not recorded, a health fact
  missed, a compliance or health fact could not be read, or a fact the table asserts was not
  recorded.
- **not followed**: a compliance fact read false, or a fact whose requirement missed. The agent did
  not do what the prompt asked, so the facts that depend on it say nothing.
- **known**: every remaining deviation is a known failure whose defect was recorded.
- **passed**: every asserted fact matched.

The command exits 1 only when some trial **failed**; `known`, `not followed` and `not measured` are
reported and exit 0.

`--summary-md` writes one row per trial with its case, harness, verdict (and why it was not
measured) and every fact that decided it, as `<fact> [<step>]: <recorded> vs expected <matcher>`.
`--summary-json` writes `job_name`, `is_failed` and, per trial, the `verdict`, `case_id`, `harness`,
`not_measured_reason`, and `failed_facts`, `not_recorded_facts`, `known_facts`,
`not_followed_facts`, `unmet_preconditions` and `unexpectedly_passing_facts` -- each fact with its
`fact_name`, `step_name`, `status`, `is_compliance`, `is_health`, `recorded` value, the `expected`
matcher after the trial's overrides, its `known_failure` and its `unmet_requirements`. A fact's
status is one of `passed`, `failed`, `not_followed`, `precondition_not_met`, `not_recorded`, `known`
and `unexpectedly_passing`.

**The computed blocks** are written as `<summary stem>-facts.json` beside whichever summary path
was given, on every run and not only under `--record-only`: a table asserts a handful of facts, and the
rest is the reading a report aggregates later. Each trial holds its `case_id`, `harness` and
`not_measured_reason`, and one entry per step with that step's `facts` and the names of its
`not_recorded_facts`. `--record-only` is how a live job is read, and how a past job directory is
read after the fact.

**Live invariants.** `configs/diagnostics/live_invariants.json` names no case and so applies to any
trial: every live cell is read against it after `check-run`. It asserts a `prep.stage_reached` of
`conversation`, `case.readable` (the step's `instruction.md` parses, which is what decides which
records were due), `tools.every_call_has_one_result`, `steps.boundary_markers_match_case`, a
`transcript.source` of `workspace`, `harness.detected_is_lane_harness` (which compares which harness
ran rather than how the two records spell a compound name), `manifest.readable`, the health fact
`listing.complete`, and `workers.no_phantoms`, which requires that health fact, since a listing that
did not reach every provider cannot speak for a worker it does not name. `prep.stage_reached` is the
table's own top-level requirement, so a trial that never got to its conversation reads as that one
miss rather than as every invariant missing on a workspace that was never there. A miss is named in
the report's details for the cell and gates nothing.

**The fact families** the checker computes today, one block per step:

- **state, arm and seed**: `case.readable`, `prep.stage_reached`,
  `arm.harness_config.{harness,model_choice_switch,is_model_confirmed}`, `entries.count`,
  `decider.call_count`, `seed.build_status`, `seed.in_head`, `snapshot.bytes`, `snapshot.readable`.
- **transcript and tools**: `transcript.{source,user_step_count,agent_steps_with_model_name}`,
  `tools.every_call_has_one_result`.
- **steps**: `steps.boundary_markers`, `steps.boundary_markers_match_case`,
  `steps.boundary_markers_followed_by_opening`, and the ones that only mean something where they
  attach -- `steps.entries_before` on a stepped case, `steps.trajectory_cumulative`,
  `steps.driver_log_step_local` and `steps.spend_deltas_sum` from the second step on.
- **usage**: `usage.tokens_present`, `usage.is_cost_complete`.
- **manifest and the declared checks**: `manifest.readable`, `evidence.errored_entries`,
  `evidence.statuses`, `http.<check>.status` and `.reason`, `files.<check>.matched`,
  `test_commands.exit_codes`.
- **timing**, on a case that declares a [`timing`](#outcome-verification) block:
  `timing.turn_index` (the client message whose reply satisfied the goal-holding client),
  `timing.seconds_within_elapsed` (the measured span is positive and sits inside the trial's
  `elapsed_seconds`), and `timing.agrees_with_feed` (the workspace's own event feed shows the
  exchange the span was measured over). The span itself is re-derived at check time with the
  collector's own reader, over the `entries` and `turns` records in `state.json`, so the first two
  facts say what the manifest's `time_to_goal` entry was measured from; the third is the independent
  reading. The two records do **not** time the same thing and their lengths are not comparable: the
  feed stamps the last agent message, while the driver's span closes when the agent is next seen
  idle, which is everything the harness still does after that message plus up to a poll interval --
  67 seconds against the feed's 1.5 on the measured fixture trial. What they do agree on is which
  exchange was measured, so `agrees_with_feed` asks that the span opens on the client message the
  feed stamps and closes at or after the reply it stamps, with a bridge round trip of slack at each
  end. A span anchored on the welcome turn or on the trial's start is minutes out and fails it.
- **apps**: `apps.delivered`, `apps.seeded`, `apps.service_state.<name>`,
  `apps.registered_all_running`.
- **repo and bundle**: `repo.agent_commit_count`, `repo.bootstrap_commit_count`, `bundle.bytes`,
  `bundle.verified`.
- **tickets**: `tickets.step_titles`, `tickets.closed_step_titles`, `tickets.regular_titles`.
- **workers and listing**: `listing.complete`, `workers.launch_count`, `workers.captured_count`,
  `workers.embedded`, `workers.discovered`, `workers.captured`, `workers.listed_agent_created`,
  `workers.no_phantoms`, `workers.{discovered,captured,embedded}_equals_listed`,
  `workers.harness_is_lead_harness`.
- **flows**, one set per declared flow: `flow.<slug>.{status,record_kinds,png_count,
  verifier_call_count,reaction_counts,no_reaction_share,ref_step_count}`,
  `flow.<slug>.step.<k>.{reaction,observed_kind,addressed_by_ref}` per performed action, and
  `flow.<slug>.after.<k>.{checkboxes,checked,url_query}` for the page each action left behind.
- **what the verifier made of the step**: `trial.completed`, `gates.held`, `gates.results`,
  `harness.detected`, `harness.detected_is_lane_harness`, `judge.digest_flow_count`,
  `judge.screenshot_count`, `judge.unnamed_control_note`, `judge.addressed_by_ref_step_count`.

`check-run`'s four criteria are facts here (`trial.completed`, `gates.held`,
`evidence.errored_entries` and `arm.harness_config.is_model_confirmed`), so a table can declare any
of them known or override one per harness. `check-run`'s own verdict is never consulted, and
`check-run` itself stays absolute and unchanged.

The behaviour family adds its own block on a step whose case asks for the diagnostic probe, described
under [the self-diagnostic behaviour family](#the-self-diagnostic-behaviour-family) below.

### The fixture family

`configs/eval-config-diagnostics-fixture.json` is the one diagnostic case whose every answer is
fixed in the repo: it seeds the flow lab's to-do fixture (see [Seeding a case](#seeding-a-case)),
asks the agent for one literal word, and drives seven scripted flows at the seeded app. Because the
app is the repo's and the flows carry no model, the trial's records have exactly one right value,
and `configs/diagnostics/fixture_expected_facts.json` states it.

- **The case runs on the nightly claude lane**, whose `--ak` kwargs
  `configs/diagnostics/fixture_harness_config.json` spells out one per key, the way
  [the diagnose jobs](#the-diagnose-jobs) read every diagnostic harness config. It requests no proxy
  and declares no model-driven flow, so the paths under test are the ones a nightly live cell takes.
- **The agent's single turn** is a literal "reply with one word", followed by a goal entry that reply
  already satisfies. That exercises the turn loop, the welcome, the model switch, the transcript
  capture and the goal-holding client for the price of one cheap reply.
- **`min_registered_apps: 0`** is the truth about a case where the agent delivers nothing: the seeded
  row is probed and driven but never counted as delivered, so the registration criterion says so.
- **Three checks are meant to fail.** The second HTTP check, the second file glob and the second test
  command each name a nonce (`absent-7f3a`, `DIAG-absent-7f3a`) that nothing in the workspace
  carries. The table expects `failed` for them, so a reader that passes everything is red and the
  reader that decides pass from fail is exercised in both directions. Reward is a non-goal here;
  `gates.results` pins whatever the structural gates make of them.
- **The `timing` block** names the `http` class in its `requires_no_failures`, so the case's
  `time_to_goal` entry records `failed` by construction and the prerequisite path is exercised
  alongside the plain one. Its anchors are the trial's own preparation budget and timeout, and no
  fact reads them: the family asserts that the span was recorded and that the driver's record of it
  agrees with the workspace's feed, never that a turn was fast or slow.

**The table** is an ordinary expected-facts table with `case_id: "instrument"` and no known failure
declared: a fixture trial that is anything but `passed` is a defect. `prep.stage_reached` is its
top-level requirement, so a trial that never reached its conversation reads as that one miss rather
than as a failure per record a workspace that never existed could not write. It asserts the trial
reached the conversation and completed, the structural gates by name, the manifest's entry statuses, the seed
build and its presence in HEAD, the commit counts and the verified bundle, the seeded and delivered
registry sets and their service states, each declared check's structured answer, every flow's status,
record shape and frame count plus the one reading only that flow's knob can produce, what the outcome
judge was handed, the arm the transcript came through, and the timing record: the turn the goal was
met on, that its span sits inside the trial, and that the feed shows the exchange it was measured
over.

**Rerunning the live measurement.** The table states what a trial records, so it is measured, never
guessed. From a detached worktree of the branch head -- never the worktree being edited, since a
trial staged from a tree that moves under it fails later steps -- generate and run one trial:

```bash
uv run --project apps/minds_evals minds-evals generate \
  --config apps/minds_evals/configs/eval-config-diagnostics-fixture.json --output /tmp/minds-evals/datasets/fixture
just minds-evals-run /tmp/minds-evals/datasets/fixture my-fixture-run 1 \
  "--ak lane=anthropic --ak key_env=ANTHROPIC_API_KEY --ak model=haiku --ak effort=medium --ak fast=false"
uv run --project apps/minds_evals minds-evals check-diagnostics apps/minds_evals/jobs/my-fixture-run \
  --expected apps/minds_evals/configs/diagnostics/fixture_expected_facts.json
just minds-evals-cleanup-environments apps/minds_evals/jobs/my-fixture-run
```

The `--ak` flags are the fixture harness config read through `ci_matrix.diagnostic_harness_config_kwargs`
and spelled out by `ci_matrix.harbor_args_for_harness_config`, which is what the scheduled cell passes. A trial costs about a dollar and twenty minutes, and leaves one Modal
environment behind, which the last line removes.

`imbue/minds_evals/test_fixtures/diagnostics_fixture_job/` is one such job directory, trimmed to the
records the facts read, and `check_diagnostics_test.py` runs the checker over it: a table value that
no trial produces fails in PR CI rather than on the night it ships. A fact whose producer is newer
than that directory reads as not recorded there, so a row added on the back of a new record is
measured by rerunning the trial and replacing the directory -- it is a recording of one run, never a
file to hand-edit into agreement. When the directory is replaced, the copied `result.json`'s
`trial_uri` is rewritten to `file:///job/<job name>/<trial>` so that no host path is checked in, and
`git status --ignored` is checked after the copy, because the root `.gitignore` drops the `*.log`
files a trial writes.

### The self-diagnostic behaviour family

**The behaviour cells.** The behaviour family runs one trial per harness of the nightly set: the
distinct harnesses of the `is_nightly` entries of `configs/harness_configs.json`, each read off its
entry's lane through `harness_for_lane`, in the order the file first names each
(`ci_matrix.select_nightly_harnesses`). Today that is claude, pi-coding and codex. Each harness's cell
runs on the cheap config `configs/diagnostics/behaviour_harness_configs.json` names under that
harness, as the `--ak` kwargs to append to the run line:

| harness | `lane` | `model` | `effort` | `fast` |
|---|---|---|---|---|
| claude | `anthropic` | `haiku` | `medium` | `false` |
| pi-coding | `openrouter` | `openrouter/openai/gpt-5-mini` | `medium` | `false` |
| codex | `openai` | `gpt-5.6-luna` | `low` | `false` |

A cell needs its lane's key (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`) besides `ANTHROPIC_API_KEY`. An
entry may also carry `unsupported_pairs`, the pair names whose workspace template offers its lane no
pasted-key sign-in and so has nothing for the cell to measure; the codex entry names `released`, and
[`ci-matrix`](#the-diagnose-jobs) leaves such a cell out of the matrix. The file is read and
validated through the driver's own kwarg parsing on the free `resolve` job, and a nightly harness
with no entry there fails it.

**The behaviour case.** `configs/eval-config-diagnostics-behaviour.json` is one case, `behaviour`, of
two steps. `work` asks for twelve exact items, one tool call each: two step tickets and a regular
ticket through `tk`, a nonce echo, a command that does not exist, a worker launched with
`create_worker.py` from a task file the prompt spells out, and a reach for the `build-app` skill --
claude's own `Skill` tool where there is one, and otherwise a read of the skill's file, which is the
other harnesses' way of loading one and what their reader looks for. `work` is also the one step of
either family that declares a `process` block, so the collector's process checks run on a step whose
only expectation is that block: it requires `build-app` beside a nonce skill nothing invokes, forbids
a nonce skill nothing reads, and caps worker launches at the one item 10 makes, so the four process
entries record `passed`, `failed`, `passed` and `passed` by construction. `check` introduces an upload,
`configs/datasets/diag-upload` as `diagupload7f3a`, and asks the agent to read it back, echo a second
nonce and read the worker's report. Every literal carries the nonce `7f3a`, so no template text can
match one by accident. `work` declares no `min_reward`, and generation warns about that; it is
deliberate, since a floor there would abort `check` exactly when the progress reader regressed. The
case's `timeout_seconds` of 4800 splits by exchange count into 2400 s per step. Both steps set
`"diagnostic_probe": true`. To run one cell by hand (codex shown; another cell takes its own row's
flags and lane key):

```bash
just minds-evals-generate apps/minds_evals/configs/eval-config-diagnostics-behaviour.json /tmp/minds-evals/datasets/diagnostics-behaviour
OPENAI_API_KEY=... just minds-evals-run /tmp/minds-evals/datasets/diagnostics-behaviour diagnose-behaviour-codex-local 1 \
  --ak lane=openai --ak model=gpt-5.6-luna --ak effort=low --ak fast=false
```

**The diagnostic probe.** A step whose config sets `"diagnostic_probe": true` (a boolean; generation
refuses anything else) runs two extra reads, both records of the agent's own behaviour that share
nothing with the readers they are there to check. At collection, one exec prints the workspace's
`data/.tickets/*.md` files raw, the name, type and labels of every agent its own `mngr list` names,
every `data/.tasks/launch-task/*/reports/report.md` that exists, and every file under `data/uploads/`,
as sections ending in an end marker; the output is kept as `verification/diagnostic_probe.txt`, and
`diagnostic_probe.py` parses it with a parser of its own. A probe that did not answer still writes
that file, holding `failure_reason: <why>` on its first line and whatever the exec printed after it,
so an unrun probe never reads as a workspace that holds nothing. After collection the driver reads
the full input of every tool call the feed has shown from the chat app's per-event detail endpoint
(`/api/chats/<chat_id>/events/<event_id>/detail`), in one exec, and records each payload in
`driver_events.jsonl` as an `event_detail` record.

**The behaviour facts.** `behaviour_facts.py` computes them at check time, from the step's own
records, and folds them into the fact block of any step whose case config asks for the probe. Every
other step omits them, so a fixture trial carries none. The table that asserts them is
`configs/diagnostics/behaviour_expected_facts.json`, which grades the case `behaviour` and requires
`prep.stage_reached@work` throughout, so a cell that never got through preparation reads as that one
miss rather than as every fact failing.

They come in three kinds, and the table marks which is which:

- **Compliance** (`compliance: true`) says whether the agent did what the prompt asked:
  `agent.step_tickets`, `agent.regular_ticket`, `agent.ran_nonce_echo`, `agent.ran_missing_command`,
  `agent.worker_launched`, `agent.worker_finished`, `agent.read_upload_marker` and
  `agent.invoked_skill`. Each is read only from the probe (**D**) or the event feed (**E**), whose
  parsers share nothing with the readers
  under test, so a regression in the tickets capture, the agent listing, the captured trajectory or
  the verifier's renderers can never read as "the agent did not comply". A compliance fact that
  reads false makes the cell *not followed*, and the instrument facts that depend on it are left
  unasserted.
- **Health** (`health: true`) says whether those sources answered at all: `probe.read` (the file is
  there, carries no `failure_reason` line, and parses), `feed.inputs_read` (every tool call the feed
  shows had its input read, which `feed.tool_calls_without_input` counts) and `listing.complete`. A
  health miss is *failed*, never *not followed*: a source the instrument could not read is never the
  agent's doing.
- **Instrument** facts are agreements between records written independently of one another, and each
  requires the compliance and health facts its reading depends on.

Which record each fact is read from:

| record | what it is | facts |
|---|---|---|
| **D** the probe | `verification/diagnostic_probe.txt`, parsed by `diagnostic_probe.py` | `probe.read`, `agent.step_tickets`, `agent.regular_ticket`, `agent.worker_launched`, `agent.worker_finished`, `steps.upload_marker_present` |
| **E** the event feed | the `feed_event` and `event_detail` records of `driver_events.jsonl` | `feed.*`, `agent.ran_nonce_echo`, `agent.ran_missing_command`, `agent.read_upload_marker`, `agent.invoked_skill` |
| **T** the tickets | `verification/tickets.jsonl` | the ticket half of `progress.*` |
| **A** the trajectory | `agent/trajectory.json` | `tools.nonce_in_one_executing_result`, `failures.missing_command_is_error`, `steps.upload_marker_read`, `workers.model_is_lead_model`, `process.invoked_skills`, `process.worker_launch_count`, and the trajectory half of `progress.step_ids_agree` |
| **R** the derived outputs | `verifier/derived/{judge_transcript.txt,progress_summary.json,harness_failures.json}` | `progress.declared_titles_agree`, `progress.done_summaries_agree`, `progress.nonce_block_count`, `progress.rendered_block_count`, `progress.regular_ticket_rendered`, `failures.missing_command_count` |
| **W** the worker captures | `verification/workers/captures.json` | `workers.report_captured` |

The `process.*` pair runs the readers the collector's process checks run -- `scan_skill_invocations`
and `scan_worker_launches` -- over the captured document, while the manifest's process entries hold
what those same readers made of the captured *stream*, so the two are a second record of each other:
`evidence.statuses@work` pins the four process entry ids and their statuses -- beside `file_inventory`,
which the always-on capture records on every step whatever the case declares -- and a reader that
stopped seeing the skill in one of the two records reds the cell.

`tools.nonce_in_one_executing_result` reads a tool result the way the verifier's renderers do, by the
tool that produced it, and so does the trajectory half of `progress.step_ids_agree`. Both are about a
command's output, so only an executing tool's result can carry it. `steps.upload_marker_read` instead
reads the marker in any tool result of step 2: the prompt asks the agent to read the upload, not to
run a command, and a harness whose file-reading tool answers that item (pi's `read`) puts the text in
a result no executing tool produced. `behaviour_facts.EXECUTING_TOOLS` is the checker's own copy of
the set both renderers filter on, so computing a fact never executes a verifier template;
`behaviour_facts_test.py` pins it against each renderer's, because a tool name added to one and not
the other would silently drop that harness's command output from every fact read through it.

**How a cell is graded.** `check-diagnostics <job_dir> --expected
configs/diagnostics/behaviour_expected_facts.json` gives the cell one verdict, by the rules under
[Checking a diagnostic run](#checking-a-diagnostic-run). The table spells the healthy value at each
entry and the defect, with its mark, in a `by_harness` override, so the known failures are readable
as a list of what is broken and where:

| fact | harness | recorded | why |
|---|---|---|---|
| `failures.missing_command_is_error` | codex | `false` | mngr's codex converter marks no result errored, so a failing codex command reaches the failure scan only through its text |
| `transcript.agent_steps_with_model_name` | codex | `"none"` | [#898](https://github.com/imbue-ai/mngr-internal/issues/898) |
| `arm.harness_config.is_model_confirmed` | codex | `null` | [#898](https://github.com/imbue-ai/mngr-internal/issues/898): no model on a codex step, so nothing can confirm the switch |
| `usage.tokens_present@check` | codex | `false` | the feed carries no usage for a codex turn |
| `steps.spend_deltas_sum@check` | codex | `null` | [#898](https://github.com/imbue-ai/mngr-internal/issues/898): harbor records no per-step input tokens for a codex step, so the deltas cannot be summed at all |
| `workers.model_is_lead_model@check` | claude, pi-coding | `false` | [#1008](https://github.com/imbue-ai/mngr-internal/issues/1008): the worker runs the template's pinned model whatever the lead was switched to |
| `workers.model_is_lead_model@check` | codex | `null` | [#898](https://github.com/imbue-ai/mngr-internal/issues/898): the worker's steps name no model either |

Every known failure is strict, so a cell that stops recording the defect is *failed* and the mark has
to come off. While any of them is declared, the best verdict a cell can have is *known*, which is
what all three record today.

`behaviour_facts_test.py` grades each harness's own live cell, whose job directory is checked in
under `test_fixtures/diagnostics_behaviour_jobs/`, trimmed to the records the facts read. The table
and those cells are one measurement, so a wrong table entry and a fact that reads the wrong record
both fail in CI rather than on the night. When a cell is replaced, the copied `result.json`'s
`trial_uri` is rewritten to `file:///job/<job name>/<trial>` so that no host path is checked in, and
`git status --ignored` is checked after the copy, because the root `.gitignore` drops the `*.log`
files a trial writes.

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
arms**: each `(mngr, default-workspace-template)` pair times each **suite** -- one eval config and
the [harness configs](#harness-and-model-arms) worth running it on. One cell is one arm of one
suite, and the cell is the unit of everything below -- what runs, what is skipped, what is
reported, and what is remembered green.

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

### Which suites a run evaluates

`configs/nightly_suites.json` is the checked-in list of suites, and it is the single place that
names what a night runs. Each entry is one eval config plus the arms to run it on, and the file as
checked in is:

```json
{
  "suites": [
    { "config": "apps/minds_evals/configs/eval-config-small.json", "harness_configs": [] },
    {
      "config": "apps/minds_evals/configs/eval-config-time-to-mock.json",
      "harness_configs": [
        { "name": "opus-standard" },
        { "name": "codex-sol-low" },
        { "name": "codex-terra" }
      ]
    }
  ]
}
```

An **empty (or absent) arm list means every entry the harness configs file marks `is_nightly`**,
which is what a config worth running against everything wants. A **named list runs exactly those
arms whatever their `is_nightly` flag says**: `opus-standard` is too expensive to run against every
config and is still worth one suite naming it. An arm may also carry `"attempts"`, a positive whole
number that becomes harbor's `-k` -- how many times each case is run, for a measurement one sample
is too noisy to read. It defaults to 1, which is the only count that costs nothing extra, and the
flag is passed only above it.

A config is named by repo-relative path, has to be in the checkout, and is spelled into job and
artifact names as its **config slug**: the file name, lowercased, without the extension
(`eval-config-time-to-mock`). It is held to the same shape and length a harness config name is, two
suites may not name one config, and two configs whose file names shorten to one slug are refused --
their cells would share every job, artifact and summary name. `minds-evals ci-matrix` checks all of
that on the free `resolve` job, and a unit test checks the checked-in file on every test run.

`configs/harness_configs.json` is the checked-in list of named harness configs, and it is where
the per-arm decision lives: every entry carries an `is_nightly` flag, and that flag is the set a
suite naming no arm of its own runs. `default`, `haiku`, `pi-gpt-5-mini`, `codex-sol-low` and
`codex-terra` set it; `pi-haiku`, `pi-glm-4.7-flash`, `codex-astra-low` and `opus-standard` do not,
so they run only where a suite names them or a dispatch asks for them -- which is how
`opus-standard` runs nightly against `eval-config-time-to-mock` and against nothing else.

For a suite that names no arm, the file's order is the order that suite's cells are decided in, and
so the order of the grid's columns in the Slack report -- which is why arms worth reading against
each other, `haiku` beside `pi-haiku`, are listed side by side. A suite that names its arms is
decided in the order it wrote them, for the same reason. Everything else in an entry is one of the
run line's own [kwargs](#harness-and-model-arms), with the same default an unset kwarg has, so
`default` -- a lane and nothing more -- is the product exactly as it ships.

The codex configs name their model rather than leaving it unset, because codex has no fixed
default: it sorts its catalog by the catalog's own priority and takes the first model the picker
would show, so the model a config left unset would run on moves whenever the template's pinned
codex version does (under codex 0.154.0 it is `gpt-6-astra`). Each then names the effort that
model's own catalog entry defaults to, which is why they differ: `codex-sol-low` is `gpt-5.6-sol`
at `low`, `codex-terra` is `gpt-5.6-terra` at `medium`, the same family's everyday model, and
`codex-astra-low` is `gpt-6-astra` at `low`, the model and effort codex 0.154.0 picks for itself.
The first two are nightly; `codex-astra-low` runs only when a dispatch names it. Each is the lane
exercised at one price point, not a controlled comparison against the others: they differ in
effort as well as model, and codex's effort ladder does not line up with claude's or pi-coding's
rung for rung.

Every nightly codex cell reports on the **main** pair alone for as long as no release carries the
template method they need. Every suite is run against every pair, and the `openai` lane's
pasted-key sign-in is newer than the `stable` channel's `minds-v<version>` tag, so a released-pair
codex cell has its sign-in refused on every trial and reads as failed, naming the lane. It is the
cell that fails and nothing else: the suite's other cells and the whole main pair are unaffected.
Reading a red codex column on the released pair as a regression is the mistake to avoid until that
tag carries [the template the lane needs](#harness-and-model-arms).

A name matches `^[a-z0-9][a-z0-9.-]*$` (dots are in, because a model version is part of what names
an arm), is at most 30 characters, is unique in the file, is never `oracle`, and is never
`diagnose` nor starts with `diagnose-`: it labels a harbor job, a concurrency group, an artifact and
a line of the Slack report, beside the oracle's and the diagnose jobs' own. The same words are
refused as an eval config's slug, which reaches the same names from the other side. A cell's whole
label, `<pair>-<config slug>-<arm>`, is capped at 64 characters, so that two cells of one suite stay
legible where a job title is elided. Every entry is validated through the driver's own kwarg parsing
on the free `resolve` job, selected or not, so a config the driver would refuse at construction
fails before any paid runner starts, and a unit test validates the checked-in file on every test
run.

### The jobs

- **`resolve`** (free) freezes each pair's refs to SHAs, lists the green markers once with
  `gh cache list --key minds-evals-green-`, and runs `minds-evals ci-matrix` to decide the cells:
  which pairs run, which cells run, and what each cell's run line and marker key are.
- **`oracle`**, one entry per (pair, eval config) with at least one running cell, replays the canned
  transcript: it exercises the box image build, generation, the verifier container and grading
  without booting Minds or paying for the agent, and it gates the expensive pass. Cheap is not free
  -- grading is the verifier's judge call, so an oracle-only run still costs one judge pass per case
  (which is why an oracle run asserts `reward >= 0.8` rather than exactly 1.0). **The oracle pass
  boots no workspace, so it is independent of the harness config**: it runs once per suite rather
  than once per cell. It is not independent of the eval config -- the task it replays a transcript
  against and the criteria that grade it are that config's own -- so each suite has an oracle of its
  own, and a suite's live cells run only after it passed.
- **`evaluate`**, one entry per running cell, is the real eval, at a concurrency equal to the
  config's case count times the arm's attempts, so every trial runs in one wave. A cell fetches only
  its own secrets, downloads the oracle summary of its own (pair, eval config) and refuses to start
  unless that oracle passed, regenerates the same dataset at the same SHAs (so the image build is a
  Modal cache hit for the other cells of its suite), runs `just minds-evals-run` with its harness
  config's `--ak` flags appended and `-k` where its suite asked for more than one attempt, writes
  its green marker as soon as the live pass has been checked green, and only then deletes the
  environments it recorded and uploads its artifacts, so a cleanup the Modal API refused turns the
  job red without costing the arm a re-run.
- **`diagnose-fixture`**, one entry per resolved pair, and **`diagnose-behaviour`**, one entry per
  resolved pair and harness of the nightly set, run the two
  [self-diagnostic families](#checking-a-diagnostic-run) beside `evaluate`. They check the eval
  itself -- its readers, collectors and verifier -- rather than the product. They belong to the pair
  rather than to one of its eval configs, so they ride on the pair's first message. See below.
- **`notify`** posts one Slack message per pair and eval config through `minds-evals ci-report`, and
  writes the same reports to the run summary.

`minds-evals check-run` decides both passes: it passes only when every trial completed, no trial
carries a harness `error` status, the structural gates hold, and no trial that asked for a model is
recorded as having answered on another -- so a cell goes red on a model switch that did not take.
**Judge scores are reported, never gated** -- they are statistical, and one run's number is not a
regression signal.

The oracle job's concurrency group is `minds-evals-oracle-<pair>-<config slug>` and a cell's is
`minds-evals-<pair>-<config slug>-<harness config>`, so two runs never evaluate the same arm at
once. The harbor job names are `<pair>-<config slug>-oracle-<run_id>` and
`<pair>-<config slug>-<harness config>-live-<run_id>`.

### The diagnose jobs

`ci-matrix` decides the diagnose jobs of every resolved pair, **whatever its cells decided**: the
diagnostics write no green marker and read none. A night with no commits moves neither pair's SHA,
and the `released` pair's SHA does not move when `apps/minds_evals` does, so those are exactly the
nights on which the diagnostics are the only measurement taken. Each entry carries the pair's refs
and SHAs, the lane key variable, and the `--ak` flags built from its family's harness config --
`configs/diagnostics/fixture_harness_config.json`, or the entry for its harness in
`configs/diagnostics/behaviour_harness_configs.json` -- through the driver's own kwarg parsing. Both
files are validated on the free `resolve` job, and a nightly harness with no behaviour config fails
it. The behaviour harnesses are those of the nightly set whatever a dispatch's `harness_configs`
selects.

A behaviour entry may also name `unsupported_pairs`: the pairs whose workspace template offers its
lane no pasted-key sign-in, so the cell has nothing to measure there. `ci-matrix` leaves that cell
out of the matrix and lists it under `diagnose_unsupported` instead, and the Slack report names it.
No box is spent to produce a dark cell, and no expected table carries a per-pair exception. The
`codex` entry names `released` today.

Both jobs need `resolve` alone and run unless the run stops after the oracle (`skip_live`, or a push
without `[live]`) or no pair resolved. They are **not gated on the oracle**: the oracle validates a
different dataset and runs only when some cell does, so on an all-green night it is skipped, and
anything behind it would be too. They **gate nothing** either, and a red one cancels no other.

Each job mirrors a cell: it fetches `ANTHROPIC_API_KEY`, the Modal token pair and its lane's key,
writes the throwaway Modal profile, mints its `ci-` user-id prefix, generates its family's case
(`configs/eval-config-diagnostics-fixture.json` or `-behaviour.json`) at the pair's frozen SHAs, runs
it with `just minds-evals-run` at a concurrency of one, and checks it with `minds-evals
check-diagnostics` against `configs/diagnostics/fixture_expected_facts.json` or
`behaviour_expected_facts.json`. **The job fails only when some trial is `failed`**; `known`,
`not followed` and `not measured` are reported and leave it green (as for a cell, a harbor run that
crashes, or a Modal deletion that was refused, turns it red too). It then deletes the environments
its job directory records -- the stepped layout included -- and uploads its summary and job
directory. `diagnose-fixture` also runs the age-based backstop sweep, once per pair, because it runs
on all-green nights when the oracle does not. Both are budgeted at 200 minutes like the cells: the
behaviour case's worst case is about 140 minutes (two steps of 2400 s conversation, 900 s
verification, 300 s grace and a 600 s verifier).

| | fixture | behaviour |
|---|---|---|
| job title | `diagnose fixture (<pair>)` | `diagnose behaviour (<pair> x <harness>)` |
| concurrency group | `minds-evals-diagnose-fixture-<pair>` | `minds-evals-diagnose-behaviour-<pair>-<harness>` |
| harbor job name | `<pair>-diagnose-fixture-<run_id>` | `<pair>-diagnose-behaviour-<harness>-<run_id>` |
| summary artifact | `minds-evals-summary-<pair>-diagnose-fixture` | `minds-evals-summary-<pair>-diagnose-behaviour-<harness>` |
| summary files | `diagnose-fixture-summary-<pair>.{md,json}` | `diagnose-behaviour-summary-<pair>-<harness>.{md,json}` |
| job artifact | `minds-evals-jobs-<pair>-diagnose-fixture-<run_id>` | `minds-evals-jobs-<pair>-diagnose-behaviour-<harness>-<run_id>` |

The job artifacts exclude `agent/snapshots/`, as a cell's do.

**Live invariants.** A handful of facts are true of every trial whatever its case, and
`configs/diagnostics/live_invariants.json` declares them. Every live cell is read against that table
in the same step as `check-run`, off the same job directory, and writes
`live-invariants-<pair>-<config>.{md,json}` beside its own summary. The read **gates nothing**: its
exit code is discarded, and a miss is named in the cell's details in the Slack report. The cell's
trials run one case against one table, so the line names each missed invariant once with the trials
that missed it (`live invariants missed: prep.stage_reached (3 of 3 trials)`) rather than repeating
a trial's whole list per trial; a cell that missed none has no line. That is what turns "the readers
are checked once a night on a fixture" into "the readers are checked on every real trial".

### Green markers, and what they promise

A cell that passes end to end is recorded green in an `actions/cache` marker keyed on
`minds-evals-green-<pair>-mngr-<mngr sha>-dwt-<dwt sha>-cfg-<config path slug>-hc-<harness config name>-<12-hex digest>`,
and the next night skips it. The digest is over the harness config's own kwargs, so editing a config
re-runs its cells under the same name and leaves every other cell green. The key deliberately does
not carry the suite's attempt count: that says how many samples a night buys of an arm, not which
arm ran. A cell is skipped when its exact key is among the markers the run may restore -- those
saved on the run's own ref or on the default branch -- and a suite whose every cell is skipped runs
no oracle pass either. A red cell saves nothing, so it retries on the next slot. Dispatch with
`force` to run a cell anyway. List the markers with `gh cache list --key minds-evals-green-`; the
Caches web UI cannot filter by key prefix.

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
- `harness_configs` -- comma-separated harness config names, which **override the arms of every
  suite the run evaluates**: a dispatch naming `haiku` runs `haiku` against each selected config,
  whatever arms that config's suite would have run. Empty (the default, and what a schedule and a
  branch push get) runs each suite as checked in; a name that is not in the harness configs file
  fails the `resolve` job.
- `config` -- one ad-hoc eval config to evaluate **instead of** the checked-in suites, on the arms
  `harness_configs` named or, naming none, on the nightly set. Empty (the default) runs every suite
  in `configs/nightly_suites.json`. Its persona count becomes the run concurrency.
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
`notify` the Slack report instead), and artifacts are kept for 14 days: per (pair, eval config),
`minds-evals-summary-<pair>-<config slug>-oracle` and
`minds-evals-jobs-<pair>-<config slug>-oracle-<run_id>` from the oracle pass; per cell,
`minds-evals-summary-<pair>-<config slug>-<harness config>` and
`minds-evals-jobs-<pair>-<config slug>-<harness config>-<run_id>` from the live pass. A summary
artifact holds `oracle-summary-<pair>-<config slug>.{md,json}` or
`live-summary-<pair>-<config slug>-<harness config>.{md,json}`, named so that `notify` can merge
every one of them into a single directory without two suites overwriting each other. `oracle` is
never a harness config name, which is what keeps an oracle pass's artifacts from colliding with the
cells' of the same suite. The `-jobs-` artifacts hold the full job directories minus
`agent/snapshots/` -- the workspace tarball is ~90 MB of a trial's ~92 MB.

The `notify` job posts **one Slack message per pair and eval config**, rendered by
`minds-evals ci-report` -- the pairs answer different questions ("is what we are about to ship
healthy?" and "is what users are running healthy?"), the suites measure different things, and a
reader acts on one of them at a time. A message opens with a header naming the pair, the config and
their verdict (passed, failed, skipped, not evaluated, or broken: the worst of that suite's oracle
pass and its cells), then a line carrying the pair's refs, SHAs and oracle verdict beside the run's
duration and trigger. A pair the run skipped whole, or whose refs did not resolve, has no suite to
report and gets a single message of its own. It posts under `:big_brain:` when every arm it reports
came out green -- every cell passed, every cell was skipped as already green, or an
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
from the cells. A suite of a running pair whose every cell is already green reads as
`skipped (already green)` and keeps its columns: the run buys an oracle pass only where a cell gates
on one, so there is no oracle verdict to report for it, and that is what most nights of a settled
matrix look like. `:heavy_minus_sign:` is a pass that was never attempted, skipped because it is
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

**Diagnostics** are reported for every pair the run decided them for, a pair whose every cell was
skipped included, and not at all on a run that stopped after the oracle. The pair's opening line
gains the worst verdict over its diagnose jobs -- failed, then a job that left no summary (broken),
not measured, not followed, known, passed -- naming every job at that verdict:
`oracle passed; diagnostics failed: behaviour codex (3 facts)`, or `diagnostics passed` when every
one passed, with any unsupported cell in brackets after it. A **`failed`** diagnostic makes the pair
`failed` and the message `:brainless:`, since it is the instrument's own fault. **`passed`, `known`,
`not followed` and `not measured`**, and a broken diagnose job, leave the pair's verdict as its cells
made it. Each failing fact is a row of the failed-trials table, with `diagnose fixture` or
`diagnose behaviour <harness>` in the config column and `<fact> [<step>]: <recorded> vs expected
<matcher>` as the note (prefixed `unexpectedly passing:` for a strict known failure whose defect was
not recorded). The details block names a diagnose job that left no summary, then each not-measured
trial's reason and each not-followed trial's missed compliance facts, the behaviour cells the pair
cannot run at all, the **live invariants** each cell's own trials missed, and, last, every known
failure with the issue or reason it gives. A diagnostic fails every fact of a broken reader, so the
failures table keeps to Slack's 100-row cap and the table character budget, whole rows first-come,
and says how many rows it dropped. A red diagnose job whose summary explains it -- a failed or a
broken diagnostic -- does not raise the warning for a job that went red with every arm green.

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
`mngr/ci/OPENROUTER_API_KEY` to exist in Vault, and the two codex cells need
`mngr/ci/OPENAI_API_KEY`; without it such a cell fails at the fetch step, before a box is built,
and the other cells are unaffected.

### CI environments and cleanup

A CI run passes `--ak user_id_prefix=ci-<YYYYMMDDtHHMMSSz>-`, so every Modal environment it creates
is attributable to one run and one wall-clock time. Teardown is three-layered:

1. The driver destroys its own nested workspace sandboxes in a `finally` block.
2. `minds-evals cleanup-environments --job-dir <job dir>` deletes exactly the environments that job
   recorded, and runs even when the pass failed. This layer runs in **every** job -- oracle, cell and
   diagnose alike -- and collects the pass it is part of. It reads the state of a stepped trial's
   archived steps as well as the trial root's, so the behaviour diagnostic's two-step trials are
   collected like any other.
3. `minds-evals cleanup-environments --sweep-prefix <ci prefix> --older-than-hours 8` is the backstop
   for a job that died before recording anything. It runs in the **oracle** job, so once per (pair,
   eval config), and in **`diagnose-fixture`**, once per pair (which is what runs it on the
   all-green nights the oracle skips), and deletes only names carrying a scheduled run's
   `ci-<timestamp>` stamp. Two of those passes overlapping is harmless: a name one has already taken
   reads as gone rather than as a failed deletion. **The cutoff is `now - 8 h`
   and the budget of every job that creates an environment is 3 h 20 m, so the sweep never collects
   the run it runs inside, nor the cells that run after it** -- an earlier run's leak is collected
   by a later run, usually the next night's. That is the point of the 8 h: it puts every
   still-possible eval, including the cells running alongside, and every developer's work out of
   reach.

Cancelling a job (or hitting its timeout) SIGKILLs harbor rather than unwinding it; the boxes it
leaves then ride their own timeouts -- 3 h for the nested workspace sandboxes, and for the harbor
sandbox 4 h in a live pass, which is where `just minds-evals-run` sets `--ek sandbox_timeout_secs`
(the oracle pass calls harbor directly and takes its default) -- and the cleanup step still runs.
