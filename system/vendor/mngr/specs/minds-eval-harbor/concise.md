# Convert the Minds Persona Evals to the Harbor Framework

## Overview

* `apps/mngr_minds_eval` was a bespoke harness that ran persona-driven multi-turn chat evals against real Minds workspaces on Modal, with results in R2 and a Claude judge for scoring. It has been removed; this spec is the record of what replaced it.
* This spec converts it to a [Harbor](https://github.com/harbor-framework/harbor) eval (harbor 0.21.0). The design goal is to stay as close to vanilla harbor as possible: every custom concept that harbor already models is replaced by the harbor-native equivalent -- job runner, task format, Modal environment provider, agent API, rewardkit verifier, results layout, and viewer. (Correction: harbor is pinned via its upstream git tag, not `harbor[modal]` from PyPI; see Implementation corrections for why.)
* The work ships as a stack of three PRs: (1) a new app adding the harbor eval alongside the existing one, (2) a side-by-side comparison of both harnesses, (3) removal of the old harness.
* All load-bearing mechanisms were smoke-tested on Modal before this design was written: plain single-container tasks (oracle reward 1.0, 45s), docker-compose via DinD (reward 1.0, 63s), and nested Modal sandbox creation from inside a harbor environment via `[environment.env]` token passthrough (reward 1.0, 31s).

## Decisions

These four forks were decided with the user before writing this spec.

* **Topology: nested Modal sandboxes.** The harbor environment (single container, direct mode) runs the Minds box; the box creates one nested Modal workspace sandbox per case through the production path (`POST /api/v1/workspaces` -> `mngr create` -> Modal provider). What enables this is passing the Modal token pair into the box: smoke-tested via `[environment.env]`, implemented via the backend-start exec env (see below), which uses the same Modal secret plumbing. Of the three viable options this has the smallest surface: no product-code changes, near-verbatim reuse of the existing box image, and ~150 lines of topology-specific glue (mostly moved code). The alternatives were rejected: a single fat container requires a LOCAL launch mode that does not exist in `apps/minds/imbue/minds/primitives.py:LaunchMode` (product feature work), and DinD compose disables all network policy, forces host networking, and needs a per-trial in-sandbox build or registry pipeline for the large box image.
* **Driver: a host-side custom harbor agent.** A `BaseAgent` subclass in the new app owns the whole conversation loop; the in-workspace eval worker in the default-workspace-template repo is no longer used and is retired via the companion dwt PR opened (and merged immediately) in PR3.
* **Operating model: CI and local development, equal weight.** The design must work as a scheduled CI regression job and as an ad-hoc local run; both consume harbor's `jobs/` output.
* **Verifier: pure rewardkit.** The three judged dimensions become rewardkit likert criteria and rewardkit owns the judge prompts, so the PR2 comparison against the old judge is approximate rather than exact -- explicitly accepted.

## Concept mapping

| existing concept | harbor concept |
|---|---|
| persona case | one task directory (generated) |
| batch | one job (`harbor run -p <dataset dir>`) |
| `launch <name>` | `harbor run ... --job-name <name>` |
| box (Minds computer) | the task's environment (Modal sandbox) |
| workspace | nested Modal sandbox created by the box (unchanged production path) |
| eval worker (in dwt repo) | host-side persona-driver agent (`-a` import path) |
| `evaluate` | rewardkit verifier (`tests/`), re-runnable via `harbor trial regrade` |
| `inspect` / `list-batches` | `jobs/<job>/result.json`, per-trial dirs, `harbor view` |
| R2 transcript + state.json | `/logs/agent/` transcript + ATIF `trajectory.json` + trial `result.json` |
| restic per-turn snapshots | per-turn workspace tarballs in trial artifacts |
| `timeout_seconds` | `[agent].timeout_sec` |
| per-batch Modal env + `USER_ID` scoping | per-trial `MNGR__PROVIDERS__MODAL__USER_ID` set by the driver when it starts the backend |
| batch config in R2 | `jobs/<job>/config.json` + `lock.json` (plus the generated dataset dir itself) |

## New app

* Location: `apps/minds_evals` (package `imbue/minds_evals/`, console script `minds-evals`).
* Modules:
  * `generate.py` -- the task generator (adapter pattern): reads the existing eval-config JSON schema unchanged (`mngr_branch`, `dwt_repo`, `dwt_branch`, `timeout_seconds`, `personas[]`) and emits one harbor task directory per persona case into a dataset directory.
  * `driver.py` -- `MindsPersonaDriver(BaseAgent)`, the host-side conversation loop.
  * `decider.py` -- the `DECIDE_FROM_PERSONA` role-play call (ported from the dwt worker's `eval_decider.py`: same prompt framing, `claude-opus-4-8`, `max_tokens=64`, fallback literal `"Sounds good."`).
  * `minds_bridge.py` -- helpers that reach the box's Minds HTTP API and the workspace's system_interface through `environment.exec` (ported from `minds_client.py`).
  * `templates/` -- task templates: `task.toml`, `instruction.md`, `tests/` (rewardkit), `solution/` (oracle).
* The CLI surface is deliberately minimal to stay harbor-aligned: `minds-evals generate --config <f> --output <dir>`, plus a justfile recipe that prints/invokes the full `harbor run` command. There is no wrapper around `harbor run` itself.
* `--output` is required and datasets are generated outside the repo tree (the `minds-evals-generate` recipe defaults to `/tmp/minds-evals/datasets/generated`), because each task embeds a full mngr-internal clone and one under `apps/` trips the repo's marked-test discovery; `apps/minds_evals/datasets/` is gitignored as a safety net for an in-tree `--output`. Datasets are disposable -- dev runs and CI regenerate them from the checked-in configs.

## Task generation

* The generator resolves `mngr_branch` to an exact SHA at generation time (port of `box.remote_tip`) and records it in each task's `[metadata]`. `dwt_branch` is pinned the same way (`dwt_sha`), and the box clones that SHA, so a dataset's workspace template is fixed at generation time rather than at trial time.
* Each task directory is named after the case id; `[task].name = "minds-evals/<case-id>"`.
* `instruction.md` carries the persona and prompt list in prose plus a fenced JSON block with the full case config (persona, prompts, `timeout_seconds`, `mngr_sha`, `dwt_repo`, `dwt_branch`, `dwt_sha`). The driver parses that block out of the `instruction` argument to `run()`, which is necessary because custom harbor agents do not receive the task directory.
* The same case data is also written to `tests/case.json` for the verifier's programmatic checks (expected turn counts).
* `environment/` is identical across all tasks in a dataset: an adapted copy of the box `Dockerfile` and `entrypoint.sh` (owned by the new app) plus a staged shallow clone of mngr-internal at the resolved SHA (port of `box._fetch_mngr_source`).
* The old harness's in-box app overlay (`box._stage_app_overlay`, and the Dockerfile's `rm -rf`/`COPY` of the harness package) is dropped from the adapted Dockerfile: the driver is host-side, so no harness code runs inside the box.
* Because the environment context is byte-identical across tasks, Modal's image-layer cache builds the box image once per mngr SHA and every other task in the job reuses it (`Image.from_dockerfile` builds on Modal's builders, same as today's `box.ensure`).
* Each task directory carries its own ~50 MB mngr clone (roughly 400 MB on disk for an 8-case dataset); Modal deduplicates the upload by content hash, so only local disk pays for the copies.
* Per-case data must never leak into `environment/`, or the cache key diverges and every task rebuilds the image.
* `task.toml` template highlights:
  * `[environment]`: `cpus = 6`, `memory_mb = 16384`, `workdir = "/work/mngr"`.
  * `[agent]`: `timeout_sec = <timeout_seconds from config>`.
  * `[verifier]`: `timeout_sec = 600`, `environment_mode = "separate"`, `env = { ANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}" }` (host-templated; `harbor run` needs `-y` or interactive approval for it).
  * `[metadata]`: `mngr_branch`, `mngr_sha`, `dwt_repo`, `dwt_branch`, `dwt_sha`, case id.
* There is no `[environment.env]` section: the driver starts the backend itself (see below) and supplies all secrets and per-trial env at that exec, which exec'd processes inherit.
* The driver parses the Modal token pair out of `~/.modal.toml` on the host (direct port of `box._modal_token_env`), so nothing secret is required in the shell environment or in task files.

## The environment (box)

* The box image is the new app's adapted copy of the box `Dockerfile` with its desktop stack; the entrypoint is NOT auto-started (harbor keeps its default `sleep infinity` keepalive).
* The driver's `setup()` starts the backend itself: `environment.exec("setsid nohup /usr/local/bin/entrypoint.sh > /logs/agent/box.log 2>&1 < /dev/null &", env={...})`.
* The env supplied at that exec carries everything the box needs: `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` (parsed from the host `~/.modal.toml`), `ANTHROPIC_API_KEY` (host env), `MNGR__PROVIDERS__MODAL__USER_ID` (below), `MINDS_ENV=staging`, `MINDS_MODAL_EXTRA_TEMPLATE=modal_eval`, and `MINDS_BOX_MNGR_REF=<mngr_sha>`. **Correction (PR1):** the dwt `modal` template carries no `pass_host_env` of its own, so the key is named in `MINDS_EXTRA_PASS_HOST_ENV` (which the in-box minds backend turns into `--pass-host-env` per create). That alone was insufficient: dwt pins claude agents to shared config-dir mode, in which mngr_claude skips the create-time step that pre-approves the key, so the workspace's claude chat agent deadlocked on Claude Code's custom-API-key TUI dialog. The manifest therefore also forwards `MNGR__AGENT_TYPES__CLAUDE__ISOLATE_LOCAL_CONFIG_DIR=true` (a config-override that outranks the dwt settings file), which restores the approval. See the Implementation corrections section.
* `USER_ID` is the sanitized trial name (derived from `logs_dir`, `jobs/<job>/<trial>/agent`) plus a fresh per-`run()` salt: harbor trial names already carry a random shortuuid, and the salt guarantees that re-runs or resumes can never collide even if a trial name repeats, since the old harness's atomic `modal environment create` uniqueness claim has no harbor equivalent.
* Starting the backend from `setup()` rather than the image entrypoint is what makes the Modal-provider `USER_ID` unique per trial, so each trial's Minds discovery only ever sees its own workspaces. This replaces the old per-batch Modal environment isolation; no `modal environment create` is needed anymore.
* `setup()` then polls for the backend port (port of `minds_client.discover_api_port`, driven through `environment.exec` reading `/proc/net/tcp`) before returning. There is no `[environment.healthcheck]` because the service starts in the agent phase, after env-level healthchecks run.
* Agent setup has a 360s default timeout in harbor; the job config sets the agent-level `agents[].override_setup_timeout_sec` high enough for Electron plus backend boot (measured in PR1).
* Run-scoped knobs: `--ek sandbox_timeout_secs` is set to agent timeout + verifier timeout + 30 min slack; `-n` controls concurrent boxes (each is 6 CPU / 16 GB, so the default of 4 is a reasonable cost ceiling for dev runs).
* The watchable noVNC desktop URL does not survive the conversion (harbor's Modal provider opens no tunnels); debugging uses `harbor task start-env -e modal -i` and `modal shell`. Nothing replaces the watchable desktop the old app's `box` utility gave (tracked on #708).

## The driver agent

* Invocation: `uv run --project apps/minds_evals harbor run -p <dataset> -a imbue.minds_evals.driver:MindsPersonaDriver -e modal -n 4 -y ...` from the monorepo root.
* `harbor[modal]==0.21.0` is a pinned dependency of `apps/minds_evals`, so `uv run --project apps/minds_evals harbor` gets the right harbor version AND makes the driver import path resolvable; a bare `uvx harbor` would run in an isolated env that cannot import the app.
* `apps/minds_evals` is a standalone uv project rather than a member of the monorepo's uv workspace: harbor's `rich>=14.1.0` and `modal>=1.5.1` floors cannot co-resolve with the workspace's `litellm[proxy]` (`rich<14`) and `modal==1.4.3` pins, and uv allows one version per package per workspace. It therefore carries its own `uv.lock`, and its tests and type check run under `just test-minds-evals` via a dedicated path-gated CI job instead of the root offload run.
* `run()` implements the existing turn semantics exactly:
  1. Create the per-case dwt clone inside the box (port of `launch._ensure_base`/`_prepare_clone`, minus writing `test_case_metadata.json`, which only the retired in-workspace eval worker consumed) and create the workspace through the Minds API (`workspace.build_payload` semantics unchanged: `launch_mode=MODAL`, `backup_provider=CONFIGURE_LATER`), polling the create operation to `agent_id`.
  2. For each prompt: wait until the workspace agent reaches `WAITING`, send the turn (literal, or `decider.py` for `DECIDE_FROM_PERSONA`), and wait for the reply. That wait is a bridged poll: `environment.exec` into the box, then mngr's remote-exec path into the workspace, then curl against the workspace-local system_interface -- the same API the old worker polled (the exact mngr CLI invocation is a PR1 verification item).
  3. After each completed turn, snapshot the workspace `/mngr` dir as a tarball (same exclude set as the old restic job) into `/logs/agent/snapshots/post_message_<k>.tar.gz` via the bridge; snapshot cadence is a driver kwarg (`--ak snapshot_mode=per-turn|final|off`, default `per-turn`).
  4. Append every event to `/logs/agent/full_transcript.jsonl` after each turn (same event schema as today), and maintain `/logs/agent/state.json` (`waits_done`, `num_turns`, `test_state`) so a timed-out trial still leaves a gradeable partial transcript.
  5. Also emit an ATIF `trajectory.json` with `source: "user"` / `"agent"` steps so `harbor view` renders the conversation.
* `-m/--model` selects the decider (simulated-user) model, defaulting to the currently pinned `claude-opus-4-8`; this gives the harbor-conventional model flag a real meaning.

### Turn sources

* Each entry in a case's `prompts` list resolves to a turn source -- the object that produces the user's next message for that turn:

```python
class TurnSource(ABC):
    """Produces the simulated user's message for one conversation turn."""

    @abstractmethod
    def next_message(self, case: CaseConfig, transcript: Transcript) -> str: ...


class LiteralTurnSource(TurnSource):
    """Deterministic: returns the config's literal prompt string verbatim."""


class PersonaLLMTurnSource(TurnSource):
    """Non-deterministic: renders the persona plus the transcript so far into
    the role-play prompt, calls the decider model, and falls back to the
    literal "Sounds good." on any error (ported from eval_decider.py)."""
```

* The generator-facing config keeps today's encoding: a literal string maps to `LiteralTurnSource`, and the `DECIDE_FROM_PERSONA` sentinel maps to `PersonaLLMTurnSource`.
* The turn loop is source-agnostic: wait for `WAITING` -> `source.next_message(...)` -> send -> wait for the reply -> snapshot/record; sources never touch the environment.
* All non-determinism is confined to `PersonaLLMTurnSource`: literal turns are pure data and replay-stable, and the decider's model name and each role-played message are recorded in the transcript so LLM turns are auditable after the fact.
* Future rule-based sources (e.g. a state-machine responder keyed on the agent's last reply) implement `TurnSource` without changes to the loop, the config plumbing, or verification.
* `populate_context_post_run` fills `AgentContext`: decider token counts and cost, plus `metadata` with descriptive stats (turns completed, avg words per agent turn, timed_out).
* Cleanup runs in a `finally` block (harbor agents have no teardown hook): `environment.exec("cd /work/mngr && uv run mngr list --ids | uv run mngr destroy - --force")` tears down the trial's workspace sandboxes (`mngr destroy` has no `--all` flag; the pipe-from-`list` form is the documented destroy-everything idiom, and the box only ever sees its own `USER_ID` scope); the nested sandboxes' own `modal_eval` 3h timeout is the backstop if the driver dies.
* Timeouts: harbor wraps `run()` in `[agent].timeout_sec`; on `AgentTimeoutError` the partially-written transcript and state files are still synced and graded.
* Timed-out trials emit reward 0 with a `timed_out` marker in `reward-details.json`. The old harness instead excluded them from batch aggregates as `N/A`, so any aggregate comparison (including PR2's) must exclude marked trials to match old semantics.

## Verification

* Pure rewardkit, in a separate verifier environment (`[verifier].environment_mode = "separate"`).
* Separate mode is required for `harbor trial regrade` (harbor rejects regrade on shared-mode tasks), and it lets the heavyweight box be torn down before verification instead of idling through it.
* In separate mode `tests/` is the verifier image's build context, so the generator emits a small `tests/Dockerfile` (slim base with uv) that copies `test.sh`, `checks.py`, `judge.toml`, and `case.json` to `/tests`.
* The transcript and state files reach the verifier via declared artifacts: `artifacts = ["/logs/agent/full_transcript.jsonl", "/logs/agent/state.json"]` in `task.toml`, re-materialized at their original paths in the verifier container.
* `tests/test.sh` runs `uvx --from 'harbor-rewardkit==0.1.*' rewardkit /tests`.
* The criteria descriptions are written fresh in rewardkit's idiom but preserve the intent of the current `_JUDGE_PROMPT` dimensions ("how an AI agent talks to a non-technical client it is building software for").
* Raw judge answers and per-criterion values live in each trial's `verifier/reward-details.json`; the raw avg-word-count value also lands in `agent_result.metadata`.
* The judge model is pinned in `judge.toml`; its key arrives through `[verifier.env]`.
* Re-grading finished rollouts without re-running them: `harbor trial regrade` (replaces the old two-pass `launch` / `evaluate` split).

### Reward mapping (current metrics -> harbor reward)

* Likert normalization in rewardkit is `(raw - 1) / (points - 1)`, so a 1-10 criterion keeps today's scale exactly and is invertible for comparisons (`raw = 9 * normalized + 1`); raw values are preserved in `reward-details.json`.

| current metric | harbor criterion | type / normalization | weight |
|---|---|---|---|
| `conciseness_score` (1-10) | `judge.toml` criterion `conciseness` | likert `points = 10`, `(s-1)/9` | 1.0 |
| `nontechnical_language_score` (1-10) | `judge.toml` criterion `nontechnical_language` | likert `points = 10`, `(s-1)/9` | 1.0 |
| `proactive_score` (1-10) | `judge.toml` criterion `proactive` | likert `points = 10`, `(s-1)/9` | 1.0 |
| `avg_word_count` (reported, unscored) | `checks.py` wordiness guard: passes unless avg words per agent turn exceeds `avg_word_count_baseline * 1.1` (the negated-criterion idiom: scores the behavior the agent should NOT exhibit) | binary, 0 or 1 | 1.0 |
| only `finished` cases scored (`N/A` otherwise) | `checks.py` structural gates: transcript parses, agent engaged with distinct non-stub replies, all turns completed, not timed out; a failed gate zeroes the reward via `finalize.py` (see Implementation corrections -- rewardkit's `required_pass` cannot express this) and is marked in `reward-details.json` | binary gate | gate (no weight) |

* `reward` = weighted mean of the four scored criteria above, gated by the structural checks; with equal weights the wordiness guard is 25% of the reward, which is the primary knob to adjust at review time.
* `avg_word_count_baseline` is written into `tests/case.json` by the generator; its default is an unmeasured seed, and it is overridable per config, so the way to ground it is to measure the mean over a batch of real runs and set it there.
* `judge.toml` sketch:

```toml
[judge]
judge = "anthropic/claude-opus-4-8"   # current judge model, kept for parity
files = ["/logs/agent/full_transcript.jsonl"]

[[criterion]]
name = "conciseness"
description = "..."   # ports the current conciseness dimension
type = "likert"
points = 10

# nontechnical_language and proactive follow the same shape
```

## Oracle

* `solution/solve.sh` writes a canned near-perfect transcript (and matching state/snapshot placeholders) into `/logs/agent/`, without booting Minds, so `harbor run -a oracle` exercises generation, environment build, verification, and results end-to-end.
* After the first box-image build on Modal's builders (10-20 min, then layer-cached), oracle runs complete in minutes.
* Because the judges are LLMs, oracle runs assert `reward >= 0.8` rather than exactly 1.0 (deviation from the adapter guideline of oracle == 100%, which assumes deterministic verifiers).

## Results, CI, and archival

* Local/dev: results live in `jobs/<job-name>/` (per-trial dirs with `result.json`, transcripts, snapshots, and `verifier/reward-details.json`) and are browsed with `harbor view`.
* CI: a scheduled job generates the dataset from the checked-in config, runs `harbor run`, and archives the `jobs/<job>/` directory as a build artifact.
* The run recipe uploads nothing: `jobs/<job>/` stays on the machine that ran it. Archival is the scheduled runner's job, with its own credentials; no bespoke storage layer remains.
* The new harness needs none of R2, restic, `setup-r2.sh`, per-batch Modal environments, or `scripts/modal_nuke.py`.
* Dropped semantics (accepted): fire-and-forget launches, since the harbor runner must stay up (moot on CI runners), and the live noVNC desktop URL.

## PR stack

The conversion is complete: the new app landed, and the old harness has since been deleted and its
console script name taken over. The comparison PR was closed unmerged, so no `comparison.md` exists;
the old-vs-new justification lives in that PR's description.

* PR1 (`apps/minds_evals`): the new app as specified above, unit tests for the generator/driver/decider (mocked environment), an oracle-based smoke path, docs (`README.md`), justfile recipes, and changelog entries.
* PR2 (comparison): run both harnesses on the same config and mngr branch; commit `apps/minds_evals/docs/comparison.md` with the side-by-side metric table (old 1-10 judge scores and avg word count vs new rewardkit criteria), transcript spot-checks, wall-clock and cost notes, plus the small script that renders the table from old R2 results and a new jobs dir.
* PR3 (replacement): delete the old harness, rename the console script to `minds-evals`, migrate remaining references (docs, justfile, CI), and open the companion default-workspace-template PR that retires the eval worker.

## Semantics preservation checklist

| old semantic | fate |
|---|---|
| multi-turn persona chat, literal + DECIDE turns, first turn literal | preserved (driver + decider port) |
| turn gating on real agent WAITING state | preserved (same system_interface poll, bridged) |
| per-turn `/mngr` snapshots | preserved as tarballs in trial artifacts (restic dropped) |
| full transcript JSONL, partial on timeout | preserved, same schema, plus ATIF |
| per-turn progress state (`state.json`) | preserved in `/logs/agent/state.json` |
| self-completing / fire-and-forget | dropped (accepted); CI runner or open terminal |
| unique batch identity / per-batch Modal env | replaced by harbor job identity + salted per-trial `USER_ID` |
| exact-SHA reproducibility | preserved (SHA resolved at generation, in `[metadata]` + image) |
| real product creation path | preserved (identical Minds API + mngr modal provider) |
| agent auth via pass-host-env | preserved (`ANTHROPIC_API_KEY` in the box env; the dwt `modal` template's `pass_host_env` forwards it into the workspace) |
| judge scoring of finished cases only | preserved in spirit (timed-out trials score 0 with marker, excluded from comparisons via details) |
| per-case turn-count freedom | preserved (per-task instruction/config) |
| watchable desktop URL / `visit-batch` | dropped (accepted); `task start-env -i` + `modal shell` for debugging |

## Risks

* **Bridged workspace access**: the driver reaches the workspace system_interface via box-exec + `mngr ssh` + curl, so each poll is a Modal exec round trip. Poll intervals of a few seconds keep this well under rate limits, but PR1 must verify latency is acceptable end-to-end.
* **Per-trial backend boot**: every trial boots its own box (Electron + backend), adding a few minutes per trial that the shared-box design amortized across a batch. Image-build cost is amortized by the Modal layer cache; boot cost is accepted for isolation.
* **Workspace sandbox leaks**: if the harbor runner is killed hard (no `finally`), nested sandboxes survive until their 3h timeout; the driver's cleanup plus the timeout backstop bound the cost.
* **Judge nondeterminism**: rewardkit likert judges make oracle assertions and cross-run comparisons statistical, not exact; PR2 must report means over multiple trials (`-k/--n-attempts`) rather than single runs.
* **uvx cache staleness**: `uvx harbor` resolved a stale 0.5.0 in testing. Harbor is therefore a pinned uv dependency of the app (invoked as `uv run --project apps/minds_evals harbor`), and the only remaining uvx call -- rewardkit inside `tests/test.sh` -- pins its version explicitly.

## Resolved questions

* App naming: `apps/minds_evals` is approved (no PR3 directory rename needed).
* CI archival: the run recipe uploads nothing, so archival is the scheduled runner's own job, with its own credentials; see Results, CI, and archival.
* The dwt eval worker removal does not wait for a release cycle; its PR is opened and merged with PR3.

## Implementation corrections (PR1)

Where the built harness (PR #344) diverged from the design above. These correct the record; the design's decisions all held (no fork was reevaluated).

* **Workspace agent auth (the load-bearing one).** The design assumed `ANTHROPIC_API_KEY` reaches the workspace via the dwt `modal` template's `pass_host_env`. It does not -- that template has none. The key is instead named in the box-level `MINDS_EXTRA_PASS_HOST_ENV` manifest, which the in-box minds backend turns into `--pass-host-env`. But dwt pins claude agents to shared config-dir mode (`agent_types.claude.isolate_local_config_dir = false`), and in shared mode mngr_claude skips the create-time path that pre-approves the key in `.claude.json` -- so the workspace's claude chat agent deadlocked on Claude Code's "use this custom API key?" TUI dialog and produced empty transcripts. Fixed by also forwarding `MNGR__AGENT_TYPES__CLAUDE__ISOLATE_LOCAL_CONFIG_DIR=true` (an `MNGR__*` config-override that outranks the dwt settings file), which flips the chat agent to isolated mode and restores the approval. No dwt or product code change.
* **Dependencies.** harbor is pinned to its upstream **git tag v0.21.0** -- the same release the PyPI wheel carries, pinned exactly and independently of the supply-chain cooldown window that the PyPI upload was inside of when this was written. Its declared floors cannot be met inside the monorepo's uv workspace: the `[modal]` extra wants `modal>=1.5.1` against the workspace's `modal==1.4.3` pin, and harbor wants `rich>=14.1` against `litellm[proxy]`'s `rich<14` cap, and uv permits one version of a package per workspace. The app is therefore a standalone uv project with its own lock (see the driver-agent section above), which lets it take the genuine `harbor[modal]==0.21.0`.
* **Reward gating.** rewardkit's `required_pass`/`all_pass` aggregations binarize a score (`> 0`), and programmatic `.py` criteria cannot use `[scoring]` at all, so they cannot express "a failed gate zeroes an otherwise weighted-mean reward". The verifier's `test.sh` runs rewardkit and then a `finalize.py` step that composes `reward = gates_all_passed ? quality : 0`, stamps the `timed_out` marker, and -- on a judge/grading-infrastructure failure (a judge API error) -- leaves no reward file so harbor **errors** the trial rather than recording a fake 0.0.
* **Judged artifact.** The judge grades a clean per-turn `conversation.jsonl` (the eval's own turns plus the agent's replies, filtered free of tool output and framework `/welcome`/injected messages), not the raw event stream. The driver writes it alongside the raw `full_transcript.jsonl`; both, plus `state.json`, are declared artifacts.
* **Exact-SHA reproducibility.** Modal's build-context upload drops `.git`, so the staged clone ships without it and the exact mngr SHA travels as a generated `/work/mngr_sha` file (read by the driver at setup).
* **Snapshots.** The workspace tree snapshotted is `/home/user` (the home tree containing the mngr host dir), not `/mngr`. The run recipe defaults `snapshot_mode=final` (the design's per-turn default is still selectable) since a full run is a nightly job.
* **Timeouts / context.** The agent-level setup timeout is raised via `--agent-setup-timeout-multiplier` (harbor exposes no CLI flag for `override_setup_timeout_sec`). The ATIF trajectory is written at the end of `run()`, because harbor only calls `populate_context_post_run` when the agent context is still empty and this driver always populates it.
* **Operating model.** Each trial boots its own 6-CPU/16-GB box, so a full run is a **scheduled/nightly regression job, not a per-PR gate**; the run recipe takes a `concurrency` arg (set to the case count for one wave) and sizes the separate verifier env down to 2 CPU/4 GB.
* **Glue size.** The "~150 lines of topology-specific glue" estimate was low; the driver plus bridge total ~1100 lines (the bridged poll/observability, clean-conversation extraction, and cleanup verification account for most of the excess).
