# minds-evals: improvement backlog

Follow-up work on `apps/minds_evals` after PR #344 (implementation) and #336 (design spec) merged.
Companion to `concise.md`, which describes the design as built; this file tracks what we want to
change about it. Ordered roughly by how much each blocks using the eval as a standing signal.

Baseline measurements referenced below come from a local verification run on 2026-08-13 against
mngr `main@b10ae2b1f658`, dataset `eval-config-small.json`:

| run | wall clock | gates | reward | notes |
|---|---|---|---|---|
| oracle, `greeting` | 3m10s | 1.0 | 0.778 | Modal image cache was warm |
| real, `greeting` | 12m06s | 1.0 | 0.444 | 4 turns, real workspace, final snapshot, 92 MB artifacts |

---

## 1. Modal environment cleanup

**Problem.** Every trial permanently leaks a Modal environment. The driver's teardown destroys the
nested *workspace sandboxes* (`minds_bridge.destroy_workspaces`, scoped to the trial's own
`MNGR__PROVIDERS__MODAL__USER_ID`) and that part works -- verified, the trial's app shows 0 tasks
afterward. What survives is the environment itself, `minds-<minds_env>-<user_id>`, plus its deployed
`minds-staging-modal` app and its `minds-staging-modal-state` volume. `_create_environment` in
`libs/mngr_modal/imbue/mngr_modal/backend.py` creates these implicitly on the nested create; nothing
removes them.

**Evidence.** The `imbue` Modal workspace currently holds 45 `minds-staging-*` environments, ~13 of
them from eval trials (`todo-app-*` x9, `greeting-*` x2, `landing-page-*`, `compare-small*`). The
verification run above added `minds-staging-greeting-cms64y8-e9876930`.

**Why nothing catches it.** The existing CI sweeper matches only `mngr_test-YYYY-MM-DD-*`
(`libs/mngr/imbue/mngr/utils/testing.py`), which cannot match `minds-staging-<case>-<salt>`.
`scripts/modal_nuke.py` is manual and needs an explicit `-e`.

**Options.**

- Extend the driver's `finally` teardown to delete the environment after the workspace sweep. Closes
  the common case, but a SIGKILLed runner skips `finally` entirely.
- Give trial environments a name shape the date-based sweeper already understands, so the existing
  scheduled cleanup generalizes over them.
- Add a scheduled sweeper that matches the eval's naming pattern.

Preference: driver-side deletion for the normal path plus a scheduled sweeper as the hard-kill
backstop. The two are complementary, not alternatives -- see the hard-kill note under CI below.

## 2. Periodic CI so the eval does not rot

**Current state.** Nothing is wired. No workflow references `minds-evals`; the run recipe
(`private.just`, `minds-evals-run`) is dev-shaped.

**Already available.** Vault-via-OIDC supplies both secrets a run needs (`mngr/ci/ANTHROPIC_API_KEY`,
`mngr/ci/MODAL_TOKEN_ID` / `_SECRET`), and `ci.yml` already synthesizes a throwaway `~/.modal.toml`
from the token pair in exactly the shape `minds_bridge.load_modal_token_env` parses. The
scheduled-job pattern to copy is `tmr-minds-scheduled.yml` (cron + gate job + `concurrency`). The
GitHub runner only orchestrates -- all weight is on Modal -- so `ubuntu-latest` is fine.

**Blockers, in rough order of bite:**

1. **Private-repo git auth in the generate step.** `resolve_remote_tip` / `fetch_mngr_source` shell
   out to plain `git ls-remote` and `git fetch` against `mngr-internal` in a temp dir;
   `actions/checkout` credentials live in the *checkout's* local git config and do not apply. Needs
   an explicit token (URL-embedded or a global credential helper). This is the *only* private-repo
   fetch in the pipeline: `default-workspace-template` is now a public repo, so the in-box dwt clone
   in `_prepare_workspace_clone` needs no auth at all.
2. **The Modal layer cache never hits on a nightly.** The cache key is the mngr SHA, so a nightly
   against `main` resolves a new SHA every night and pays the full box-image build (10-20 min) every
   run. Structural: pin to a tag or a weekly SHA, or accept the rebuild. When the cache *is* warm the
   whole trial is 12 minutes, so this is the dominant cost term.
3. **Environment leakage compounds.** One environment + app + volume per trial, per night, forever,
   until item 1 above lands.
4. **Hard-kill leaks.** Cleanup is a `finally`. A cancelled GitHub job SIGKILLs the process, so nested
   sandboxes ride their `modal_eval` 3h timeout and the harbor box rides `sandbox_timeout_secs=14400`
   (4h, set in the run recipe). Bounded but expensive.
5. **No signal, no gate.** Nothing compares reward against a baseline or threshold, and judge scores
   are nondeterministic likert values. A useful nightly needs `-k N` plus mean-based thresholds and
   somewhere to keep history; otherwise "periodic CI" means "produces artifacts nobody diffs."
6. **Artifact plumbing is dev-shaped.** The recipe's R2 branch sources `$HOME/.minds-eval/r2.env` and
   requires `MINDS_EVAL_BUCKET` / `MINDS_EVAL_S3_ENDPOINT`, a local convention that does not exist on
   a runner. There is no `upload-artifact` step either. Sizing input: 92 MB per trial, nearly all of
   it the workspace snapshot tarball.
7. ~~**Install weight on every other job.**~~ **Done** -- while `apps/minds_evals` was a uv
   workspace member, the `uv sync --all-packages` that every CI job runs built harbor from git and
   pulled its dependency tree (fastapi, uvicorn, supabase, litellm, dirhash, pathspec) into every one
   of them. The packaging fix in section 5 keeps that tree out of the root lock entirely.

Timeout math is already sound: `--agent-setup-timeout-multiplier 3` against harbor's 360s default
gives 1080s, comfortably over `BACKEND_BOOT_TIMEOUT_SECONDS = 600`, and the agent timeout is the case
timeout plus a 300s grace.

## 3. Credentials insertion

**Current mechanism.** `minds_bridge.build_box_env` injects, only when a key is present:
`ANTHROPIC_API_KEY`, `MNGR__AGENT_TYPES__CLAUDE__ISOLATE_LOCAL_CONFIG_DIR=true`, and a
`MINDS_EXTRA_PASS_HOST_ENV` manifest naming both. The minds create path turns each name into
`--pass-host-env`, writing both into the workspace *host* env file, which every agent on that host
then inherits.

**Why the config override is load-bearing.** dwt pins `agent_types.claude.isolate_local_config_dir =
false`. In shared mode `provision()` skips `_setup_per_agent_config_dir`, which is the only caller of
`approve_api_key_for_claude` on the create path. Without that approval Claude Code challenges any
`ANTHROPIC_API_KEY` it does not recognize with an interactive TUI dialog, which deadlocks the
ready-signal wait. Flipping to isolated mode restores the approval as a side effect.

**Why it should change.** dwt owns claude auth itself and does the thing mngr's shared-mode path
skips. `system/apps/system_interface/imbue/system_interface/claude_auth.py` backs the in-UI login
modal; it writes credentials into the `env` block of the shared `~/.claude/settings.json` and calls
`record_api_key_approval`, and its docstring states credentials must go there and *never* in the mngr
host env file, because that file is frozen into supervisord and its services at boot. Production
therefore never has a key in env at create time and never meets the dialog. The host-env path was
deliberately retired from the product (`blueprint/workspace-claude-auth-settings-env/`, which removes
the AI provider from the workspace create route and scrubs keys out of `/mngr/env`); the eval
harnesses were simply missed by that migration. Every other harness -- the `minds_services`
deployment test, the Electron and Playwright e2e flows, and production itself -- authenticates
through `submit-credentials`.

**Fidelity cost of the status quo.** The eval grades an agent provisioned down a different code path
than real dwt workspaces use: per-agent config dir instead of the user's shared one, hooks baked into
a config-dir `settings.json` instead of delivered via the managed `--settings` overlay, plugin
sentinel resolution running instead of skipped, `CLAUDE_CONFIG_DIR` always exported instead of
conditional. It also breaks the invariant `.mngr/settings.toml` states explicitly -- one shared config
dir so auth, plugins, marketplaces and sessions are configured exactly once -- inside the eval
workspace, since the chat agent gets a private config dir while a bare `claude` in a workspace
terminal still resolves `~/.claude`. Style overrides do survive both paths (`_build_settings_json`
folds `settings_overrides` onto the base), so the eval is not grading a style-less agent; the delta
is narrower than the provisioning table suggests but real.

**Proposed fix.** Authenticate the way a user does, after create:

1. Poll `GET /api/claude-auth/status` until it answers -- a genuine readiness gate, which the driver
   currently lacks entirely.
2. `POST /api/claude-auth/submit-credentials` with `{"credentials": "ANTHROPIC_API_KEY=..."}`, or the
   base-URL + key blob if we want the eval routed through LiteLLM (`build_credential_blob` in
   `apps/minds/imbue/minds/desktop_client/ai_keys.py` renders it).
3. Wait for `WAITING` again -- the endpoint restarts the claude agents before returning.

The driver already has the transport: `minds_bridge.workspace_curl_json` is the same box-exec ->
`mngr exec` -> workspace-local `system_interface` bridge the deployment test uses via `docker exec`.
Closest reference implementation: `apps/minds/deployment_tests/test_litellm_via_workspace.py`.

This removes both `MNGR__AGENT_TYPES__CLAUDE__ISOLATE_LOCAL_CONFIG_DIR` and the whole
`MINDS_EXTRA_PASS_HOST_ENV` manifest from `build_box_env`, and keeps the workspace in production's
shared-config regime.

**Caveat to check before committing to it.** `submit_credentials` runs through
`welcome_resender.check_and_resend_welcome`, so the `/welcome` message may be re-sent as part of
sign-in. The driver anchors reply detection on the event count captured before each send, which
should absorb an injected message, but turn-1 anchoring is worth verifying against a real run rather
than assumed.

**Spec corrections that follow.** `concise.md` still lists "agent auth via pass-host-env | preserved"
in the semantics-preservation table; that claim was inherited verbatim from the old harness's
comments and is false. Separately, its parenthetical that only vultr/gcp/azure/docker declare
`pass_host_env` is wrong: of the four such lines in dwt's settings, only gcp and azure forward
`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `GH_TOKEN`, and `imbue_cloud` forwards only
`MNGR_PREFIX`. vultr, aws, docker and lima forward nothing. The `[create_templates.modal]` template
carries none, which is what the eval's own comment claims and is correct.

## 4. Scoring and fixtures

1. **The wordiness baseline is an unmeasured placeholder.** `DEFAULT_AVG_WORD_COUNT_BASELINE = 120`,
   and its comment says "seed value ... until PR2 measures real old-harness batch averages". PR2
   (#353) is closed, so the seed is now permanent. It is one of four equally-weighted quality
   criteria -- a quarter of the reward -- set by a number nobody measured. The verification run failed
   it by 2.8 words (134.8 against a 132 threshold). Either measure a real baseline or stop gating on
   it.
2. **The gated word metric may be the wrong one.** The guard uses `average_words_per_turn`, computed
   over the agent's messages *merged* per client turn (134.8 in the run above), while
   `average_words_per_message` (33.7) is recorded for observability only. An agent that narrates
   progress across many individually-short messages fails the merged metric while passing the
   per-message one. The judge independently noticed the same tension, scoring conciseness 7 with
   "individually concise, though several are needless narration". Decide which behavior we actually
   want to penalize.
3. **The oracle fixture is positional, not content-aware.** `_oracle_events` picks one of three
   hardcoded replies by turn index and never looks at what the client said. On `greeting`, whose
   opening turn is "hi what can you do", the canned opening reply ("On it. I'll set everything up")
   ignores the question, and the judge scored `proactive` 4/10 -- landing the oracle at 0.778, under
   the `>= 0.8` the README documents for oracle runs. Nothing tests that threshold, so the README
   claim is aspirational. Either make the fixture case-aware or drop the claim; as it stands the
   oracle floor is case-dependent and cannot serve as a smoke gate.
4. **One `greeting` turn is wasted by construction.** Turn 2 is a literal "Sounds good." sent after
   the agent has offered a numbered menu, so the agent replies "Which one? Pick a number (1-4)". The
   case config's literal turns do not fit the conversation the agent actually opens with.
5. **The stub-reply heuristic should become a real readiness gate.** `_STUB_REPLY_PATTERN` in
   `gates/checks.py` is a fragile regex standing in for "the agent is wedged". With
   `GET /api/claude-auth/status` polled before turn 1 (item 3 above), the wedged-auth case is caught
   at its cause instead of inferred from reply text after the fact.
6. **Missing gate criteria are treated as failure.** `_gates_all_passed` scores an absent criterion as
   a failed gate, silently producing a 0 reward. This is inconsistent with the judge-error path, which
   deliberately leaves no reward file so harbor errors the trial rather than recording a fake 0. A
   missing gate is a grading defect, not a bad agent, and should be handled the same way.

## 5. Packaging and workspace hygiene

1. **`override-dependencies = ["rich>=13.9.4,<14.0"]`** in the root `pyproject.toml` is
   workspace-global. uv overrides replace every requirement on the package across the whole graph
   rather than narrowing it, so harbor's declared `rich>=14.1.0` floor becomes a claim the resolver
   can no longer check -- a resolution error converted into a potential runtime error. It exists
   because harbor wants rich >= 14 and `litellm[proxy]` (via `apps/modal_litellm`) caps rich < 14,
   which is genuinely unsatisfiable. Benign today (harbor's rich usage is `Console` / `Table` /
   `Live` / `Progress` / `Group`, all long-stable in 13.x) but not self-policing at the next bump.
2. **`modal==1.4.3` against harbor's declared `modal>=1.5.1`** is the same problem without even an
   override to record it: the app skips harbor's `[modal]` extra and substitutes the workspace's
   modal, running harbor's Modal provider a full minor below its floor, invisibly to tooling. The
   paths the eval exercises demonstrably work; everything it does not exercise is unverified, and a
   harbor bump can start calling a 1.5 API without any resolution failure.
3. **The public mirror diverges silently.** `mirror/overlay/uv.lock` records no overrides and
   `apps/minds_evals` is not in the mirror's allowlist, so the public tree resolves rich
   unconstrained -- today also 13.9.4, by coincidence. The moment anything public wants rich >= 14 the
   locks drift and the mirror gate reads it as a routine lock diff.
4. **Proposed fix for all three:** take `apps/minds_evals` out of the uv workspace, giving it its own
   isolated project and lock, invoked via `uv run --project apps/minds_evals`. harbor then gets its
   declared rich and modal, the root override disappears, the filelock/platformdirs bumps stop
   rippling into the `image_requirements.txt` files and the mirror lock, and every other CI job stops
   installing fastapi/supabase/litellm. Cost: the driver import path needs the monorepo packages
   visible, solvable with a path dependency on what it actually imports (`imbue-common`).

**Resolved, as proposed.** `apps/minds_evals` is a standalone uv project: the root
`[tool.uv.workspace]` excludes it, and it carries its own `pyproject.toml`, `uv.lock`,
`.python-version` and `.venv`, resolved under the same two-week cooldown policy as the root, stated as a
rolling `exclude-newer` window so nothing has to advance it (a meta-ratchet holds the window equal to
`DEPENDENCY_COOLDOWN`). It
takes the genuine `harbor[modal]==0.21.0` and resolves rich 15.0.0 / modal 1.5.2, both above harbor's
floors; the root `override-dependencies` entry is gone and the root lock drops 14 packages with no
version change to anything that remains. The monorepo packages it needs come in as editable path
sources rather than workspace members, so trial costs are still priced off this repo's
`mngr_usage` table.

The bill for isolation is that nothing in the root workspace's tooling reaches the project any more:
`uv sync --all-packages` does not install it, the offload image does not contain it, root pytest
ignores it via `collect_ignore_glob`, and the root `ty check` excludes it. What replaces that is
`just test-minds-evals` (sync from its own lock, `ty check`, pytest) plus a path-gated
`test-minds-evals` CI job that runs the recipe whenever the app *or any of the in-repo packages that
land in its venv* changes -- a meta-ratchet checks that gate against the editable sources in the
project's own lock, since a gate missing one reports green for code it never built.

One caveat worth keeping in view: this buys a resolver-checkable claim, not a tested one. harbor ran
on rich 13.9.4 (below its floor, smoke-verified) and now runs on rich 15.0.0 (above its floor,
equally unverified by us). Item 2's substantive worry -- that a harbor bump starts calling a modal
1.5 API -- is what actually goes away, because the resolver will now refuse rather than shrug.

## 6. Iteration friction

1. **The box is built from a remote mngr SHA.** `mngr_branch` is resolved to an exact SHA at generate
   time and the environment stages a shallow clone at that SHA, so local edits to `libs/mngr` never
   reach the box: you must push a branch, regenerate, and pay a fresh 10-20 minute image build for
   each new SHA. This is the main tax on iterating against mngr-side changes, and it is worth
   considering whether a "mount local mngr" dev path is feasible without breaking the cache-key
   discipline (the `environment/` tree must stay byte-identical across a dataset -- verified that it
   is).
2. **dwt is not pinned at all, and that is a reproducibility hole.** `mngr_branch` is resolved to an
   exact SHA at generation time and recorded in the task metadata, but `dwt_branch` is carried
   through as a plain branch name and `_prepare_workspace_clone` runs `git clone --branch <branch>`
   inside the box *at trial time*. So the same dataset, run twice a week apart, silently builds its
   workspaces from two different templates, and nothing in the trial record says which one. Given
   that dwt is where the settings file, the system image, and the agent-type declarations live, it is
   the input most likely to move a score. Resolve `dwt_branch` to a SHA at generation time the way
   `mngr_branch` already is, record it in `[metadata]`, and clone that SHA. See section 9 for the
   concrete change that makes this urgent.
3. **Datasets must live outside the repo.** Each generated task embeds a ~52 MB mngr clone, so
   datasets default to `/tmp/minds-evals/...` and `test_meta_ratchets.py` had to become
   gitignore-aware. Regenerable, but easy to get wrong.
4. **The driver's state is an implicit state machine** of roughly 20 mutable `_` attributes. Worth
   restructuring if we extend the turn loop much further.
5. **The judge and decider models are pinned in two places** (`quality/judge.toml` and
   `decider.DEFAULT_DECIDER_MODEL`), both currently opus-tier. That is a cost and latency lever for
   any high-`-k` nightly.

## 7. Unfinished business from the original arc

1. **Two harnesses coexist.** `apps/mngr_minds_eval` is still in the tree; PR3 (#354), which deletes
   it, is closed. PR #344's own description says the plan was to close it "to unify the two evals for
   now", but it merged. Decide whether the old harness goes, stays as a comparison baseline, or gets
   an explicit deprecation note.
2. **The comparison writeup does not exist in the tree.** `apps/minds_evals/docs/comparison.md` is
   referenced but absent; the old-vs-new justification, including the conciseness 5.7 -> 8.3 finding
   that motivated grade-time transcript rendering, lives only in closed PR #353's description.

## 8. Switchyard / FSR reconciliation

A parallel thread explores dynamic model routing. Nothing from it exists in this repo yet -- it is all
analysis and proposals -- but it lands on `apps/minds_evals` as the instrument, so the two arcs need
to be planned together rather than merged after the fact.

### 8.1 The two things

**FSR** is `fairly-simple-router`, a standalone local repo (`~/work/fairly-simple-router`, imbue-ai
org, two files) that wraps headless Claude Code and routes by estimated task difficulty. It installs
`PostToolUse`/`PostCompact` hooks, forks the live session to ask "how difficult is the remaining
work?", maps the answer onto `very low -> haiku / low -> sonnet / medium -> opus / high -> fable`, and
switches by **halting the session** (`{"continue": false}`) so the parent wrapper can relaunch with
`--resume --model`. Working prototype, and known by its author not to be improving results yet.

**Switchyard** is NVIDIA NeMo's Rust routing proxy (external, Apache 2.0, explicitly pre-alpha and
"not for production use"). It routes and translates between OpenAI Chat, Anthropic Messages and
OpenAI Responses formats. It is a strict superset of FSR's idea implemented at the API layer:
a **stage router** that reads recent tool activity per turn, an **LLM classifier** with session
affinity, an **escalation router**, and a **random router** for weighted A/B splits.

### 8.2 Why this belongs in this backlog: the harbor convergence

FSR depends on `harbor>=0.20.0`; `apps/minds_evals` pins `harbor==0.21.0`. FSR already carries a
script that aggregates costs for harbor jobs. Routing experiments can therefore run as harbor tasks
in the same job runner this eval uses, rather than as a separate bespoke harness -- and
`MindsPersonaDriver` is host-side, so the component that decides what to send already lives outside
the workspace, which is exactly where a routing experiment wants to be.

### 8.3 What minds already provides -- do not rebuild it

- **Live model switching already ships.** `POST /api/agents/<id>/model` with `{"model": "sonnet"}` on
  the workspace's `system_interface` sends `/model <model>` down the same interactive path as a chat
  message, so a running session applies it immediately and Claude Code persists it. FSR's
  halt-and-resume mechanism is therefore unnecessary inside minds, and does not transplant anyway:
  nothing owns the interactive tmux-hosted `claude`, so `{"continue": false}` would just stop the
  agent in front of the user.
- **This is the same transport the credentials fix uses.** That endpoint sits on workspace-local
  `localhost:8000` next to `/api/claude-auth/*`, which means `minds_bridge.workspace_curl_json`
  already reaches both. A routing arm and the auth fix in item 3 share one bridge.
- **A routing policy has a template.** `fast_mode_policy.py` in dwt is a complete worked example: a
  decision file under `data/.state/`, a layered resolver, and a writer that patches the per-agent
  managed settings file so the decision survives restart. A `model_policy.py` beside it would be a
  near-mechanical adaptation.
- **The model catalog is hand-maintained** in `model_settings.py` (`fable`, `opus[1m]` with fast mode,
  `sonnet`, `haiku`; default `opus[1m]`), and it warns that plain `opus` silently drops the 1M window.
  FSR's `medium -> "opus"` would do exactly that downgrade, and its `high -> fable` treats a different
  model as "harder Opus".
- **Fixed-model arms need no routing at all.** Mirroring the existing fastMode override with
  `-S agent_types.claude.settings_overrides.model=<id>` at create time is a one-line change that
  yields fixed-model arms for A/B.

Things to avoid copying from FSR into a workspace: it writes credentials to a file in the work dir,
runs with `bypassPermissions`, and disallows `Agent`/`Task` tools -- none of which is acceptable in a
user workspace, where credentials belong in `data/.secrets/`.

### 8.4 The dependency that ties the two arcs together

**The credentials change in item 3 is on the critical path for routing work, not merely adjacent to
it.** Today the eval injects a raw `ANTHROPIC_API_KEY` into the box, so eval traffic goes straight to
Anthropic and never touches the LiteLLM proxy. That forecloses both things routing experiments need:

- **No insertion point.** Switchyard is introduced by pointing `ANTHROPIC_BASE_URL` at it instead of
  at LiteLLM -- a one-field change originating from `ai_keys.py` / `client.toml`'s
  `litellm_proxy_url`. An eval that bypasses the proxy has no field to change.
- **No cost attribution.** Per-workspace LiteLLM virtual keys are the only per-case spend signal
  available (see 8.5).

Switching the driver to `POST /api/claude-auth/submit-credentials` with the **base-URL + key blob**
(`build_credential_blob`) rather than a bare key resolves both, and is the same call the auth fix
already proposes. Open question to settle when implementing: how each eval case obtains a virtual key,
since `ai_keys.py` mints per-workspace-host keys from the desktop client, a path the eval does not
currently travel.

### 8.5 Cost measurement is a prerequisite, not a caveat

Routing is a cost/quality tradeoff, so the eval must be able to measure cost -- and today it cannot,
for a specific reason: **the `claude` usage source records dollars, not tokens.** The
`mngr_claude_usage` statusline shim writes a single `cost_snapshot` with `total_cost_usd` and no model
and no token counts. `mngr_usage/pricing.py` exists precisely so token-only sources can be priced
centrally, but claude took the shortcut. Under routing there is nothing to override: a scalar whose
model attribution is already baked in and wrong cannot be repriced. If we route requests server-side
while cost accounting comes from Claude Code's belief about which model it used, the eval measures its
own fiction.

**Resolved, and not the way this section originally assumed.** The eval does not need LiteLLM to
measure its own spend: every agent message in the workspace event stream already carries a per-message
`usage` block and a `model`, attached by mngr_claude's common_transcript converter, so the driver's
own transcript is a complete token record. `usage.py` aggregates it, prices it with `mngr_usage`'s
table, and reports the workspace agent into harbor's `n_input_tokens` / `n_cache_tokens` /
`n_output_tokens` / `cost_usd` plus the trajectory's `FinalMetrics`, with the four-bucket breakdown in
`usage.json` and `metadata.workspace_usage`. The decider is priced separately under
`metadata.decider_usage`, which also retires the defect where its tokens were reported as the agent's.

Three things follow from the source being the transcript rather than the proxy. A timed-out trial
still accounts for its spend, because the events are already in hand. Already-captured trials can be
priced retroactively. And, unlike everything else in this section, it needs **no** credential change:
the LiteLLM dependency in 8.4 is about the *routing insertion point*, not about measurement.

**A proxy in front of the workspace is still the goal**, for three things the transcript cannot give:

- **A metering boundary that cannot be evaded.** dwt pins shared claude config, so every agent in a
  workspace -- the chat agent, `Agent`-tool subagents, and worker agents created by `mngr create` --
  reads the same settings env block and inherits the same credential. Metering there needs no list of
  delegation routes, which matters because the route that actually fires in practice was not the one
  worth guarding against (see below).
- **Cache analysis in more depth than the transcript carries**, including per-request rows and the
  `LiteLLM_Daily*Spend` aggregates, which break out `cache_read_input_tokens` and
  `cache_creation_input_tokens` as first-class columns.
- **A place to tighten the box's egress.** With a proxy of our own, `api.anthropic.com` need never be
  reachable from the workspace at all: LLM traffic becomes one stable hostname, which is a shape an
  allowlist can express. This does not finish the job -- on the Modal provider the workspace performs
  its whole image build at runtime, so apt/PyPI/npm/GitHub stay open, and modal's *domain* allowlist
  needs `modal>=1.5.1` against the workspace-wide `1.4.3` pin (item 5.2) -- but "nothing reaches an
  unmetered model" becomes enforceable rather than assumed.

**Decision: run the proxy inside the eval box rather than using a deployed one.** The live staging
LiteLLM deployment would work and needs no deploy, but an in-box instance keeps Modal as the eval's
only infrastructure dependency, and leaves an obvious seam to put a second proxy behind it -- a
model-swapping router, per section 8.7 -- without touching minds. It is credentialed by the same
`ANTHROPIC_API_KEY` the eval already takes from the environment: the proxy's upstream credential is
literally `os.environ/ANTHROPIC_API_KEY` (`apps/modal_litellm/app.py`), and virtual keys are minted
against a master key we generate, so none of the product's SuperTokens/connector/paid-plan issuance
path is involved. That path exists to enforce per-customer budgets and is the wrong target for an
eval.

Harness spend (decider and judge) stays direct for now and continues to be reported as metadata;
routing it through the proxy too is what would make the egress guarantee complete rather than
partial.

**What delegation actually looks like, measured.** Across the three small-dataset cases there were
zero `Agent`-tool calls. What happens instead is dwt's own turn-end `crystallize-creation` flow
shelling out to `create_worker.py launch --template subskill-worker`, in two of the three cases. So
the undercount is not adversarial behaviour an eval must defend against: it is the workspace doing
what its instructions tell it to, on any case that builds something. A worker is an ordinary mngr
agent in the same workspace, so its transcript is served at `/api/agents/<id>/events`, and summing
usage across every agent the workspace lists would close that route using calls the bridge already
makes. Worth doing regardless of the proxy -- it is cheap, and it makes `is_cost_complete` mean
something much closer to true for claude-shaped harnesses.

**Fixing the general `mngr usage` gap remains a separate project.** The claude usage shim is a
statusline hook that emits dollars with no model and no token counts, and it is not even installed in
a Minds workspace. The aggregation in `usage.py` is a reasonable prototype for the token-emitting
writer that would fix it for every claude user, but it should not gate the eval.

Fixing the general case is a real project and off the critical path: a token+model claude usage
writer, and/or the `mngr_litellm_usage` plugin that the existing
`aggregate_usage_source` hookspec already anticipates. One cheap experiment could shrink it: Anthropic
responses carry the serving model, and Claude Code's session `.jsonl` records `message.model` per turn
(FSR's own accounting already reads this). If Switchyard reports the *actual* serving model rather
than echoing the requested one, the transcript already contains ground truth.

### 8.6 The measurement risk that this harness exists to answer

**Prompt-cache invalidation may eat the savings.** Switching models mid-session invalidates the prompt
cache, and cache reads are roughly a tenth of input price, so a switch can cost more than the cheaper
model saves. This needs measuring rather than assuming, and this harness is the right place to measure
it. Note the interaction with item 6.4: a high-`-k` nightly with opus-tier judge and decider has its
own cost profile that must not be confused with the arm being measured.

### 8.7 Proposed sequencing

1. **Caching-fidelity test first** (roughly one afternoon, independent of this app): send a fixed
   prompt through LiteLLM directly and through Switchyard -> LiteLLM, then diff
   `cache_creation_input_tokens` / `cache_read_input_tokens` in the responses, and verify the
   **1M-context beta header survives** (`opus[1m]` depends on it). Switchyard translates through an
   internal representation even for Anthropic -> Anthropic, so `cache_control` breakpoints and beta
   headers may not survive the round trip. If caching does not survive, everything downstream changes.
2. **A harbor task that swaps `ANTHROPIC_BASE_URL` to Switchyard**, reusing this app's generator and
   environment wholesale, with a `random` route for weighted A/B against a fixed-model control.
3. **Only then** consider `stage_router` and whether the UI and usage divergence is worth solving.

Route on **resolved model ids, not aliases**: Claude Code resolves `opus[1m]` to a concrete id before
the API call, so Switchyard's targets must key on the ids in LiteLLM's `model_list`
(`claude-fable-5`, `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5`).

### 8.8 Open questions carried over

- **The model picker will lie** under server-side routing: minds reads
  `read_model_from_settings(claude_config_dir/"settings.json")`, which is what Claude Code *thinks*
  it is using. Acceptable for evals; needs a decision before users see it.
- **Fast mode and routing may be incoherent as a pair** -- fast mode is Opus-only and the frontend
  hides the toggle for other models, so routing away from Opus while `fastMode=true` has no defined
  meaning.
- **Switchyard is a Rust binary** while minds' proxy is a Modal ASGI app, so productionizing means a
  new container. This does not block the eval path, which can run it locally or on a dev tier.
- **Provider independence is a smaller win than it looks -- but see section 9.** minds is claude-only
  as shipped today: the system image installs exactly one agent CLI (pinned, with a provisioning-time
  version check), and the mngr plugins for other agents are vendored with no binary installed. So
  Switchyard buys provider independence *underneath* Claude Code, which is a different and cheaper
  axis than swapping the agent CLI. That premise has a shelf life: dwt PR #427 adds codex and pi as
  first-class harnesses, which makes the two axes complementary rather than substitutes.
- **Whether `/model <arg>` skips the confirmation selector is unverified.** dwt sets
  `auto_dismiss_dialogs = true` but leaves both auto-accept depths at `0`, so do not build on the
  assumption that a blocked dialog auto-clears. Related prior art worth reading before adding any
  hook: dwt disables `claude_subagent_proxy` because its `PreToolUse:Agent` hook wedges parent agents
  in a retry loop on a permanent error.

## 9. Multi-harness support is landing in dwt

**PR #427** on `imbue-ai/default-workspace-template` ("Add the claude/codex/pi harness support to the
workspace") adds **codex** (OpenAI Codex CLI, pinned 0.147.0) and **pi**
(`@earendil-works/pi-coding-agent`, 0.83.0) alongside claude in the workspace image, plus a
`harnesses/` abstraction in the system interface. It is open with CI green, 312 files and roughly
+37k lines. Context for now, not something to build on yet -- but it changes several premises above.

1. **It dates the provider-independence argument in 8.8.** minds being claude-only is exactly what
   made swapping the agent CLI expensive. This PR does that work: harnesses are declared as agent
   types and chosen at create. The "it's real work" part stays true -- 312 files and five landing
   attempts -- but it is someone else's work, and once it lands the axis is close to free for us.
2. **The create-command shape changes.** `type` is removed from every create template in favour of
   `mngr create <name> --type <harness> -t <role>` -- harness on `--type`, purpose in a role template,
   which is what stops the template list growing as the product of harnesses and roles. Roles collapse
   to chat / worker / caretaker / automation, the `worktree` and `subskill-worker` templates are
   deleted, and `[agent_types.chat]` / `[agent_types.worker]` survive only as `parent_type = "claude"`
   shims for already-created agents. The eval creates its workspace through the minds API rather than
   calling `mngr create` itself, so the blast radius is indirect, but the minds-side create builder
   changes shape and the driver's chat-agent resolution is worth re-verifying when this lands.
3. **Harness becomes a natural eval axis** -- the same persona cases across three harnesses. It is a
   coarser axis than the model routing in section 8 and complementary to it. It would need harness in
   the case config and threaded through the driver's create payload.
4. **Auth blocks that axis, and #427 explicitly does not solve it.** Its stated limitations begin "No
   auth system -- did not touch this at all." What exists is a preflight refusal: each non-claude
   harness authenticates through its own CLI, and creating an agent on a signed-out harness raises
   before creation. Both remedies are interactive -- open a terminal and run `codex`, or run `pi` and
   type `/login`. **There is no programmatic path**, so a non-interactive eval cannot authenticate
   codex or pi at all. Note the asymmetry with item 3: claude's answer, `submit-credentials`, is
   claude-specific. Codex's own plugin notes say the gated models want an API key as "the documented
   path for programmatic workflows", but no API-key plumbing is implemented. Programmatic
   non-claude auth is a prerequisite for harness-as-an-axis, and nobody owns it.
5. **Model selection generalizes, which bears on section 8.** Every harness gets a model catalog and
   writes `minds_model_state.json`, surfaced through new `GET /api/harnesses` and
   `GET /api/agents/<id>/model-options` endpoints. The picker modes differ (claude a static list,
   codex queried dynamically from its app-server per account entitlements, pi generated from
   `pi --list-models`), and switching is per-harness: claude takes `/model`, codex takes JSON-RPC to
   its app-server, pi goes through its SDK. A routing experiment keyed on claude's `/model` semantics
   should expect that surface to become harness-dependent.

**Timing and risk.** The new-agent launchers sit behind `FEATURE_FLAG_ENABLE_OTHER_HARNESSES`, off by
default and flipped with `system/scripts/flip_feature_flags.sh`. The flag gates only the launchers,
not harness support. This body of work has already landed on dwt main once and been reverted (the
revert says the in-progress harness work "was not ready to be on main"), with four intermediate PRs
closed unmerged; #427 is the fifth attempt, has review comments but no approvals, and is blocked on a
paired mngr PR because dwt vendors those mngr changes.

**Why this makes item 6.2 urgent.** Because the eval clones dwt by *branch* at trial time, the day
#427 merges is the day eval behaviour changes -- feature flag or not, since the flag gates launchers
while the settings-file restructure, the new `agent_ready_timeout`, the Node upgrade and the deleted
templates all take effect regardless. Pinning `dwt_branch` to a SHA at generation time turns that from
a surprise into a deliberate upgrade.

---

## 10. Fast mode: pricing and per-model comparability

Minds runs its chat agent in fast mode by default, and every eval trial run so far inherited it (no
`fast_mode_decision.json` exists in a first-boot eval workspace, and bootstrap defaults to enabled
when that file is absent). Confirmed on a proxied trial: all 23 of its requests were served fast.
Fast mode returns identical token counts at a premium price -- $10/$50 per MTok against $5/$25 on
Claude Opus 5 and Opus 4.8 -- and the API reports no price at all, so every dollar figure here is
arithmetic over a rate table. The eval records the tier per request and prices each tier's tokens at
its own rate; `is_cost_rate_certain` says whether the tier behind a total was observed at all, and so
whether the rate applied to it is the one it was billed at. Three things to know:

1. **Repricing is done, but only where the tier is observable.** `mngr_usage` carries a
   `FAST_MODE_PRICE_MULTIPLIER` and `compute_cost(..., is_fast_mode=True)`, and the eval prices
   each tier's tokens at its own rate. This only helps a proxied trial: without the proxy the tier is
   invisible and everything is priced standard, which for a default Minds workspace is half the real
   figure. Two consequences worth carrying forward: **every eval cost recorded before this** was
   standard-rate and should be read as a floor, and **mngr's own usage reporting** has the same gap
   in production -- minds runs fast mode by default, and nothing outside a proxy sees the tier, so
   user-facing cost numbers understate fast-mode traffic. Deciding what to do about that is a
   separate piece of work in `mngr_usage`, not here.

2. **Fast mode is Opus-only, which breaks a naive per-model A/B.** The API returns a 400 for `speed`
   on Sonnet and Haiku (verified directly: `'claude-haiku-4-5-20251001' does not support the 'speed'
   parameter`). So an Opus arm at default settings runs fast and a Haiku arm cannot, and the arms
   differ by tier as well as by model -- a cost gap between them is then partly a rate gap. Either
   pin fast mode off for all arms, or report the tier per arm and stop comparing the totals directly.
   Note the proxy masks the hard failure: LiteLLM's `drop_params` strips `speed` for a model that
   does not accept it, so a non-Opus arm silently runs standard instead of erroring. Convenient, but
   it means the downgrade is invisible unless the recorded tier is actually read.

3. **Controlling the tier is implemented in the per-model override work (PR 407);** observing it
   lives here. An arm therefore declares both its model and its tier, and the recorded tier is what
   confirms the override actually took effect rather than being assumed.

Also worth knowing when reading cache numbers: switching tier invalidates the prompt cache, so an
arm that flips fast mode mid-run pays cache writes it would not otherwise have paid.

---

## Observed product signal worth tracking

Independently of the harness work, the one real run produced a finding about the chat agent itself:
`nontechnical_language` scored **4/10**, with the judge citing "routing to the app-builder flow",
"scaffold", "scaffolded on port 8080", "generated runner", "serve the mock from the app", "headless
browser", and raw file paths in image links. Conciseness 7, proactive 8. If the eval becomes a
standing signal, this is the dimension currently furthest from target.
