# minds-evals

Harbor-based Minds persona evals. Each persona case in an eval config becomes one
[harbor](https://github.com/harbor-framework/harbor) task; a run drives real multi-turn
conversations against real Minds workspaces on Modal and grades the transcripts with a rewardkit
verifier. It replaces a bespoke pre-harbor harness, whose eval-config schema it kept unchanged.

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
   supplies no AI credentials -- so this keeps the graded agent in the same shared config-dir regime
   real workspaces run in. The paste mints a **provider account**, whose id the response carries.
4. It then **creates the workspace's chat** through `/api/agents/create-chat`, named after the
   workspace host and bound to that account, and waits for it to reach WAITING. A workspace boots
   with no chat at all -- a chat binds to an account when it is created, and a fresh workspace has
   none -- which is why the sign-in has to come first: a create issued before it is refused for want
   of an account. A create whose answer is lost is retried, and the collision that retry hits is
   resolved back to the chat the first attempt left behind.
   Being the workspace's first chat, it is the one that gets `/welcome`. `conversation.jsonl` --
   the eval's own turns, which the gates and the wordiness check read -- carries no trace of it;
   the judged transcript drops the `/welcome` trigger but keeps the greeting it draws, as its first
   agent message. Either way the driver waits for that welcome to be *answered* before turn 1. A
   new chat reports WAITING as soon as its agent is up, which is before the workspace has
   typed `/welcome` in; sending into that window would race the delivery and leave the greeting
   landing where turn 1's reply is read from.
5. It then drives the case's turns: wait until the workspace chat agent is WAITING, send the turn
   (literal, or role-played by the decider model on `DECIDE_FROM_PERSONA`), wait for the reply,
   snapshot the workspace if the cadence calls for it (the run recipe's `final` snapshots only after
   the last turn), and keep `/logs/agent/full_transcript.jsonl` + `state.json` current in the box.
6. Once the last turn is done and while the workspace is still alive, the driver runs an
   **evidence-collection** phase: it records what was actually delivered (the app registry,
   supervisord's view of it, a file inventory, HTTP probes, declared test commands, UI flows, and
   the delivered repo as a git bundle) into `/logs/agent/verification/`. It has to happen here,
   because the verifier runs after the workspace is destroyed. See
   [Outcome verification](#outcome-verification).
7. The **verifier** (pure rewardkit, separate container) scores the recorded transcript and
   evidence. See [Scoring](#scoring).

## Setup

- `~/.modal.toml` (run `modal token new` once) -- everything runs on Modal.
- `export ANTHROPIC_API_KEY=sk-ant-...` -- the decider (simulated user), the judge, and the
  credential the driver signs each workspace in with. Set `ANTHROPIC_BASE_URL` alongside it to sign
  workspaces in against a proxy instead of the Anthropic API directly; under `--ak proxy=true` it is
  ignored, because the driver signs the workspace in against its own in-box proxy.
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
  in the box against the monorepo venv (importing `mngr_forward`, `litellm`, and `playwright`), so
  this project excludes it and the root workspace checks it instead. `templates/` runs in the
  verifier container, whose only foreign import is `rewardkit` -- a dev dependency here purely so
  this project *can* check them, which it does; it is the root workspace that skips them. The
  repo-root `test_meta_ratchets.py` keeps the two configs from excluding the same path at once (it
  runs on every PR, unlike this project's path-gated job), and `rewardkit_pin_test.py` here keeps
  the dev-group `rewardkit` on the verifier container's own pin.
- Coverage omits both directories: neither runs in the dev process.

## Usage

```bash
# 1. Generate a dataset (one harbor task per persona case) from an eval config
just minds-evals-generate apps/minds_evals/configs/eval-config-small.json /tmp/minds-evals/datasets/small

# 2. Sanity-check the dataset end-to-end with the oracle (canned transcript; no Minds boot)
uv run --project apps/minds_evals harbor run -p /tmp/minds-evals/datasets/small -a oracle -e modal -y -o apps/minds_evals/jobs

# 3. Run the real eval (concurrency = simultaneous boxes; set it to the case count for one wave)
just minds-evals-run /tmp/minds-evals/datasets/small my-eval-run 3

# 4. Browse results
uv run --project apps/minds_evals harbor view apps/minds_evals/jobs

# Re-grade finished rollouts without re-running them (needs the task path)
uv run --project apps/minds_evals harbor trial regrade -p /tmp/minds-evals/datasets/small apps/minds_evals/jobs/<job>/<trial>
```

Generate datasets outside the repo tree: each generated task embeds a full mngr-internal clone, and
one under `apps/` trips the repo's marked-test discovery.

Each trial boots its own 6-CPU/16-GB box, so a full run is a **scheduled/nightly regression job,
not a per-PR gate**. Handy knobs:

- `-m/--model` selects the decider (simulated-user) model; default `claude-opus-4-8`.
- `--ak snapshot_mode=per-turn|final|off` controls workspace snapshot cadence; the run recipe
  passes `final`, and a later `--ak` wins. Extra harbor args are the recipe's *fourth* parameter, so
  `concurrency` must be given explicitly or they bind to it:
  `just minds-evals-run <dataset> <job> <concurrency> --ak snapshot_mode=per-turn`.
- `--ak verifier_model=<model>` runs the UI-flow verification agent on a different model from the
  decider (default: the decider's). Flow driving is mechanical, so a cheaper tier may do -- measure
  flow stability before changing the default.
- `--ak proxy=true` routes the workspace's model calls through an in-box LiteLLM proxy; see
  [Token and cost accounting](#token-and-cost-accounting).
- `-k/--n-attempts N` runs each case N times (judge scores are statistical; use means).

Results land in `apps/minds_evals/jobs/<job>/<trial>/`: harbor's `result.json` and
`verifier/reward-details.json` at the trial root, and everything the driver collects under `agent/`
-- `full_transcript.jsonl`, `state.json`, `snapshots/`, `usage.json`, and `verification/`. They stay
there; the recipe uploads nothing. Archiving belongs to whatever runs the eval on a schedule, which
supplies its own credentials rather than reading a developer's.

## Eval config

The checked-in configs live in `configs/`: `eval-config.json` (nine cases) and
`eval-config-small.json` (three, two of them carrying `expectations`) for quick end-to-end runs.
Both pin `mngr_branch: main`. A config naming a branch that no longer exists fails at generation
time, when the branch is resolved to a SHA -- so a config pinned to a feature branch is worth
keeping only while that branch is.

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
  **picking up new template changes requires regenerating the dataset**. Each trial's own record
  carries `mngr_sha` and `dwt_sha` too (in `state.json` and the agent metadata), so a captured trial
  says which mngr and which template produced it.
- Each `prompts` entry is one turn: a literal string sent verbatim, or `DECIDE_FROM_PERSONA` (the
  decider role-plays the client from the persona plus the transcript so far; cannot be the first
  entry).
- `avg_word_count_baseline` feeds the verifier's wordiness guard (pass unless the average words per
  agent turn exceeds baseline * 1.1). A "turn" here is one client turn's merged agent reply; the
  driver records that per-turn average as `average_words_per_turn` in the trial metadata and, for
  observability only (no gate), the finer `average_words_per_message` (words per individual agent
  message, before the per-turn merge).
- `verification_timeout_seconds` (default 1800) is the evidence-collection phase's own budget. It is
  *added* to the task's `[agent].timeout_sec` (case timeout + verification budget + grace), so
  verification never competes with the conversation for time. It is a deadline, not a reservation:
  a case with no UI flows finishes the phase in a couple of minutes and the rest is never spent.
- Each persona entry may carry an `expectations` block; see below.

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
- `deliverable` is **required**. A block with none would expand to no programmatic checks, and
  rewardkit only pools a programmatic reward when criteria exist -- so the outcome dimension would
  silently become judge-only, carrying double the judge weight of every other case.
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
  registering its port from inside the script its supervisord program runs, as the terminal and the
  owner-exec and vm-exec daemons do), unioned with the names the workspace's own
  `system/supervisord.conf` registers through its `forward_port.py --name` invocations (which covers
  a template app whose service had not registered its port yet). Measuring beats a hand-maintained
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
afterwards. Every trial is scored on two dimensions, and cases that declare
`expectations` gain a third.

- **`gates`** -- structural: the transcript parses, the agent engaged with distinct non-stub
  replies, all turns completed, the run did not time out. These zero the reward when they fail.
- **`quality`** -- three 1-10 likert judge criteria (`conciseness`, `nontechnical_language`,
  `proactive`) plus the binary wordiness guard.
- **`outcome`** (expectation cases only; the generator omits `tests/outcome/` otherwise, so
  rewardkit never emits a partial score for it) -- one programmatic criterion per declared check
  class (`app_registered`, `http_expectations_met`, `files_expectations_met`,
  `ui_flows_completed`) plus a `works_as_expected` likert judge over the rendered expectations, the
  manifest, the conversation, and the flow evidence. The conversation is in there deliberately:
  `DECIDE_FROM_PERSONA` turns are free-form, so a client who steers the build mid-conversation must
  be graded against the evolved ask.

`ui_flows_completed` scores COMPLETION: the fraction of measurable flows that carried out their
declared steps. It does not score whether the app did what a flow's `expect` describes. That is the
judge's ruling, made from the step log and the screenshots. Trial time collects; grade time
verifies.

### Reward composition

`quality = weighted mean(conciseness, nontechnical_language, proactive, wordiness guard)` -- likert
criteria normalized as `(raw - 1) / 9`, so raw judge scores stay recoverable (`raw = 9 * normalized
+ 1`; raw values are in `reward-details.json`). `reward` is that score, zeroed unless every
structural gate passed. For expectation cases it is an even split, `reward = gates_all_passed ?
(0.5 * quality + 0.5 * outcome) : 0`: a great app described badly and a great description of no app
are equally imperfect. The split is a constant, not per-case configuration -- per-case weights would
make rewards incomparable across cases.

The gate composition lives in `tests/test.sh` (`finalize.py`) because rewardkit's `reward.toml`
aggregations cannot express "binary gate zeroes a weighted mean".

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

- `judge_transcript.txt` (from `full_transcript.jsonl`): one `[USER]` block per client turn and one
  `[AGENT · message N]` block per agent message, so conciseness is judged per individual message
  rather than over the driver's per-turn merge.
- `judge_flows_digest.txt` and a flat `judge_screenshots/` (from the flow evidence, which rewardkit
  cannot reach because it expands a listed directory exactly one level and never recurses). The
  digest carries, per flow: the declared steps, the `expect` the judge is to rule on, the completion
  status, the agent's own description of the final page (evidence, not a verdict), then every step's
  action, reasoning and page state. `judge_screenshots/` holds each flow's last four frames, up to
  24 in all, each under rewardkit's 1 MiB judge limit. Both are written unconditionally: rewardkit
  renders a listed path it cannot find as a visible `[not found]` block, while an empty listed
  directory renders *nothing at all*, which is why the digest states the screenshot count instead of
  leaving the judge to infer it.

### Error versus zero

A trial only records a score when the harness could actually grade it. Grading-infrastructure
failures error the trial instead, so they are never mistaken for a legitimate 0:

- a judge API or auth error, or rewardkit not producing a parseable reward file;
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

**Delegated work is missing from transcript-sourced totals.** The events endpoint serves
main-session events only, so a subagent's turns and any work handed to a newly created worker agent
never reach the transcript, and an agent that delegates looks cheaper than one working inline. Such
trials are marked `is_cost_complete: false`, with `delegated_call_count` (exact -- `Agent` tool
calls) and `worker_launch_count` (heuristic -- Bash commands that look like a worker launch)
alongside. Treat a flagged trial's cost as a lower bound, and never compare it against an unflagged
one.

**`--ak proxy=true` closes that gap**, by routing the workspace through a LiteLLM proxy the driver
runs inside the box, signed in with a per-trial key rather than the upstream credential. The
workspace's claude agents share one credential, so every call crosses that boundary, delegated ones
included: `agent/usage_proxy.jsonl` is the complete account and becomes the source for harbor's
fields, with the transcript's own figures kept in `metadata.transcript_usage`.

**Fast mode changes the price, not the token counts.** Minds runs its chat agent in fast mode by
default, and fast mode bills the same tokens at twice the standard rate. It is chosen per request,
so a model id alone does not determine a price, and only the proxy sees which tier served one:

| `is_speed_observed` | `fast_message_count` | What `cost_usd` means |
|---|---|---|
| `true` | `0` | Exact: every request ran standard and is priced standard. |
| `true` | `> 0` | Exact: that many requests are priced at the fast-mode rate. |
| `false` | `0` | A floor. The tier was never observed, so everything is priced standard -- half the truth if the workspace was in fast mode, as by default it is. |

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

## Notes

- The box image build takes 10-20 minutes the first time on Modal's builders, then is layer-cached
  per mngr SHA. Keep per-case data out of `environment/` or the cache key diverges.
- Debugging: `uv run --project apps/minds_evals harbor task start-env -p <task> -e modal -i`, then
  `modal shell`. harbor's Modal provider opens no tunnels, so there is no live desktop URL, but the
  box still runs x11vnc/websockify for in-sandbox use.
- Cleanup: the driver destroys its nested workspace sandboxes in a `finally` block
  (`mngr list --ids | mngr destroy - --force`, scoped to the trial's own USER_ID); the nested
  sandboxes' `modal_eval` 3h timeout is the backstop if the runner dies hard.
- This app contains async code (`driver.py`, `minds_bridge.py`): harbor's agent and environment
  APIs are async, so the ratchets that normally forbid async/asyncio carry nonzero baselines here.
