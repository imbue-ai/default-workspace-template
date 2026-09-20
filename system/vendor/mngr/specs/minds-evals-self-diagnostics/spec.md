# minds_evals: the self-diagnostic suite

**Audience:** developers working on `apps/minds_evals` (the driver, the evidence collector, the verifier renderers, `check_run.py`, `check_diagnostics.py`, `ci_matrix.py`, `ci_report.py`) and on `.github/workflows/minds-evals-scheduled.yml`.

**Status:** implemented; every milestone is on this branch, and the diagnose jobs have run twice from a push (see [Milestones](#milestones)). Tracks [#934](https://github.com/imbue-ai/mngr-internal/issues/934).

The suite validates the **eval instrument**, not the agent, with two families of cases:

- The **fixture family** boots a real workspace from a commit that adds a fixed app to the template, drives every evidence check class against that app with scripted flows, and asserts exact known answers about what the trial recorded.
- The **behaviour family** prompts a cheap agent, on each harness the nightly run uses, to do precise things across two steps (track steps with `tk`, run tools, fail a command on purpose, launch a background worker, read an upload that arrives with the second step), and asserts that every independent record of that trial agrees with every other.

Every assertion is made at check time, by `minds-evals check-diagnostics`, from the job directory harbor collected.
The driver computes nothing for the suite; it only persists records, the same records the graders read.
A regression in the instrument is then red on the night it lands, instead of misreporting that night's real trials or drifting into judge scores weeks later.

Related:

- [`../../apps/minds_evals/README.md`](../../apps/minds_evals/README.md): how a trial works, what the job directory holds, and how the scheduled CI is shaped. This spec assumes it.
- [#935](https://github.com/imbue-ai/mngr-internal/issues/935): trial seeding. The fixture family is its first consumer; the cut it needs is specified below, and the general design belongs to that issue.
- [PR #932](https://github.com/imbue-ai/mngr-internal/pull/932), [PR #980](https://github.com/imbue-ai/mngr-internal/pull/980), [PR #1006](https://github.com/imbue-ai/mngr-internal/pull/1006) (merged): the flow lab and its `todo` fixture, the settle-watch and `wait` action, and ref-addressed actions with the `stale_ref` refusal.
- [PR #936](https://github.com/imbue-ai/mngr-internal/pull/936) (merged): the verifier reads codex's shell. Before it, codex command output reached neither the judged transcript nor the failure scan, and both affected cells were green: the defect class the behaviour family exists to catch.
- [#873](https://github.com/imbue-ai/mngr-internal/issues/873), [PR #876](https://github.com/imbue-ai/mngr-internal/pull/876): worker collection is reconstructed from command text; the listing is now taken unconditionally.
- [PR #898](https://github.com/imbue-ai/mngr-internal/pull/898): model and token usage on codex ATIF steps.
- [#1008](https://github.com/imbue-ai/mngr-internal/issues/1008), [#1009](https://github.com/imbue-ai/mngr-internal/issues/1009): a claude worker runs the template's pinned model whatever the lead was switched to, and model confirmation excuses it.
- [#1019](https://github.com/imbue-ai/mngr-internal/issues/1019): checks of the UI-flow verification agent itself, which this suite does not make (see [The verification agent](#the-verification-agent)).
- [#929](https://github.com/imbue-ai/mngr-internal/issues/929): cost accounting and the in-box proxy, which this suite no longer uses (see [Non-goals](#goals-and-non-goals)).
- [#793](https://github.com/imbue-ai/mngr-internal/issues/793): issue filing for the scheduled run.
- [`proxy_calibration_notes.md`](proxy_calibration_notes.md) and [`update_path_notes.md`](update_path_notes.md): findings from the first cut that are out of this suite's scope and kept for the follow-ups that need them.

## Contents

- [Background](#background)
- [Goals and non-goals](#goals-and-non-goals)
- [Shared machinery](#shared-machinery)
- [The fixture family](#the-fixture-family)
- [The behaviour family](#the-behaviour-family)
- [The verification agent](#the-verification-agent)
- [CI integration](#ci-integration)
- [Cost](#cost)
- [Milestones](#milestones)
- [Open questions](#open-questions)
- [What the suite still does not catch](#what-the-suite-still-does-not-catch)

## Background

The scheduled run's oracle pass (`harbor run -a oracle`) boots no workspace.
`solve.sh` writes a canned trajectory and `state.json`, and `evidence_collection.oracle_evidence_files` fabricates an all-passed manifest, HTTP records and a three-record flow log for cases that declare expectations.
It fabricates no screenshots, no bundle, no captured transcript, no workers, no `usage.json`, no snapshot and no `arm` block, and its one spliced tool call is spelled the claude way.
So the pass proves that generation, the verifier container and grading still fit together, and nothing else.

Everything that runs inside a box goes untouched: the Chromium launch and CDP connect, the driver's `mngr forward` instance, `resources/box_flow_step.py`, the ARIA snapshot format (which moves with playwright bumps), screenshot capture, `mngr transcript --format atif` and each harness's converter behind it, the worker discovery and capture, the model-switch endpoint behind `is_model_confirmed`, the workspace snapshot pull, and every harness-specific reader downstream of the capture.

A live cell exercises all of them, but against an app the agent under test invented that night, with no second record to check its readings against.
Two recent defects show the shape of what slips through:

- Codex runs its shell in code mode, and the verifier's readers knew only claude's and pi's tool names. A codex trial's command output reached neither the judged transcript nor the failure scan, its progress timeline was empty, the gate meant to catch that passed vacuously, and `nontechnical_status_language` scored a free 10. Both codex cells were green (fixed by PR #936).
- A launch command that failed was still counted as a worker by the command-text discovery, so a trial recorded a worker that never existed, with `is_cost_complete: false`, on two harnesses.

The flow lab (`test_flow_lab.py`) closes the executor gap from the step script down, locally.
What the lab drops is the box transport, the forward proxy, the workspace and everything the harness writes, which is precisely the layer a trial adds.

## Goals and non-goals

Goals:

- Every night, on every pair the run evaluates, run one fixture trial, and one behaviour trial per harness of the nightly set, and assert named facts about the records they produced.
- Assert **persisted evidence**: every fact is computed at check time from the job directory, so a fact can only be right if the record the graders read is right.
- Keep the assertion surface as **data**: checked-in expected-value tables, read by one checker.
- Mark facts known to fail today, so a known defect is not a red night, and make a known failure that starts passing red, which is the `xfail(strict=True)` discipline `test_flow_lab.py` states.
- Keep three things apart: the instrument being broken (red), the infrastructure never producing a workspace (not measured), and the agent not doing what the prompt asked (not followed). Only the first is never the agent's fault, and it must be neither diluted nor hidden. A record the checker could not read is the first kind, never the third.
- Run every diagnostic cell in the configuration the nightly's live cells run, so the paths under test are the nightly's.
- Protect the night's real trials: every harness a nightly config drives has its readers checked against a second record the same night, on the same pair, and a small set of invariants is checked on every live trial.

Non-goals:

- **Scoring the agent.** No fact folds into `reward`; an agent must not score zero because the instrument broke.
- **Replacing the oracle.** The oracle covers generation, the verifier container and grading for one judge pass per case; the diagnostic covers the box and the readers. Both run.
- **Measuring model quality.** Whether a cheap model follows a precise prompt is a precondition here, reported and never scored, and how well the verification agent drives a page is a question for the follow-up in [The verification agent](#the-verification-agent).
- **The in-box proxy.** The nightly's live cells run without it, so a proxied diagnostic would confirm the model through a path the nightly does not take and skip the one it does. Proxy calibration against the transcript, per-harness spend calibration and the detection of stray agents the proxy alone can see are a later family of checks, with the proxy working on every harness as a prerequisite; see `proxy_calibration_notes.md`.
- **A trend line.** The facts are hard-gated on the diagnostic cases and recorded, unasserted, on live runs, where the report can aggregate them later.

## Shared machinery

Both families share one persisted record, one checker, one kind of expected table, one set of verdicts and one CI job shape.

### The persisted record

The checker reads only files harbor collects into the job directory, step by step, under `steps/<name>/`.
The README's "Generated layout and per-step output" and "Outcome verification" sections describe them; the ones the facts read are:

| file | writer | facts |
|---|---|---|
| `agent/state.json` | driver, at every turn and once more at step end | `prep.*`, `seed.*`, `arm.*`, `entries.*`, `snapshot.*`, `decider.*`, the client messages |
| `agent/instruction.md` | driver, at step start | the case's expectations and step position, read with the driver's own parser |
| `agent/trajectory.json` | driver, the captured ATIF document at step end | `transcript.*`, `tools.*`, `steps.boundary_markers*`, `workers.embedded`, `workers.model_*` |
| `result.json` | harbor, with the driver's per-step metadata (`transcript_usage`, `workspace_usage`, the spend deltas) | `usage.*`, `steps.spend_deltas_sum`; `agent/usage.json` holds the cumulative reported usage and is read by nothing here |
| `agent/driver.log`, `agent/driver_events.jsonl` | driver | `steps.driver_log_step_local`, `feed.*`, `agent.ran_*`, `agent.read_upload_marker` |
| `agent/verification/manifest.json` and the entry files beside it | collector | `evidence.*`, `manifest.*`, `http.*`, `files.*`, `test_commands.*`, `apps.*` |
| `agent/verification/{apps.toml,services.txt,supervisord.conf}` | collector | `apps.*` |
| `agent/verification/{repo_state.json,deliverable.bundle}` | collector | `repo.*`, `bundle.*` |
| `agent/verification/tickets.jsonl`, `diagnostic_probe.txt` | collector | `agent.step_tickets`, `agent.regular_ticket`, `agent.worker_*`, `probe.*` |
| `agent/verification/workers/**` | collector | `workers.*`, `listing.*` |
| `agent/verification/flows/<slug>/log.jsonl` and frames | collector, from the box's step script | `flow.*` |
| `agent/snapshots/*.tar.gz` | driver | `snapshot.*` |
| `verifier/reward-details.json`, `verifier/derived/*` | the verifier and `keep_derived_outputs.py` | `trial.completed`, `gates.*`, `harness.*`, `progress.*`, `failures.*`, `judge.*` |

**What the producers still have to persist.**
The first cut's milestone 3 already persists structured exit codes, HTTP status codes, matched file counts, the seed record, `preparation_stage`, the tickets capture, the unconditional agent listing and the verifier's derived outputs.
The revised checker needs these additions, each a few lines in the writer named:

- `state.json` gains `snapshot_byte_count`, `decider_call_count` and `client_messages` (the texts the driver sent), beside `entries`.
- The collector writes `supervisord.conf` and the isolated-instance services section beside `services.txt`, and the manifest says whether the registry was present (`is_registry_present`), so an unreadable registry is not an empty one.
- The collector writes `workers/listing.json` beside `agents.json`, carrying the listing command's exit code and errors, since a destroyed worker is only recognised on a complete listing.
- The collector writes `workers/captures.json`: each worker's name, state and whether its stream was captured.
- A failed tickets capture or diagnostic probe writes its file with a `failure_reason` line instead of writing nothing, so "not run" and "failed" are distinct on disk.
- Each step-boundary marker in the trajectory carries the step's opening message in its `extra`.
- Every record in `driver_events.jsonl` carries a `type`, so feed events, event details and decider messages are separable without guessing.
- Each flow directory gains `run.json`, carrying the flow's status, reason and verifier call count, so a flow's outcome is readable without the manifest and the judge's renderer keeps reading `log.jsonl` unchanged.

Nothing in this list changes what a live trial does; every item is a record of something the trial already holds.

### The checker

`minds-evals check-diagnostics <job_dir> --expected <table>` reads every trial of the job through the stepped layout, computes the facts of each step from the files above, and gives each trial one verdict.
`evidence_facts.py` is the checker's module: pure functions from file contents to a flat JSON object from a dotted fact name to a value.
Nothing in the driver imports it, and no `state.json` carries a fact block.

- A fact is read from the last step that ran unless its key names a step: `<fact>@<step>` (`gates.held@work`), so a table can pin the same fact on several steps.
- A fact is **not recorded** when the record it reads was due on a step that ran and is absent or unreadable. That is a status of its own, and it fails the fact it belongs to: an instrument that did not write its record is broken, whatever the table expected. A record that was never due (a flow the case did not declare, a probe on a step that declared none, a snapshot on a trial that pulls none) leaves its facts **omitted**, as the README's rule already says; the case's own declaration, read from `instruction.md`, decides which.
- A fact is **null** when its record is there and cannot answer (a registry the collector could not parse, a probe that reports a failure). Null matches only an expected null.
- The table names its case: `check-diagnostics` refuses a job whose trials belong to another case, with an error naming both, instead of grading them.
- `--record-only` computes the facts of a job with no table and writes them beside the summary, one block per trial and step. That is the reading a live job gets, and the mode a past job directory is read with.
- `--summary-md` and `--summary-json` mirror `check-run`'s, with the verdict, `failed_facts`, `known_facts`, `not_followed_facts`, `unmet_preconditions`, `not_recorded_facts` and `unexpectedly_passing_facts` in the JSON.

`check-diagnostics` does not call `check_job_directory` as a gate in front of the table.
It reuses `check-run`'s readers and expresses `check-run`'s criteria as facts (`trial.completed`, `gates.results`, `evidence.errored_entries`, `arm.harness_config.is_model_confirmed`), so a known failure or a per-harness override can cover any of them.
`check-run` stays absolute and unchanged.
A reader `check-run` shares that maps a missing or malformed file onto a healthy value (a missing manifest reads as "nothing errored") is wrapped by a fact that says whether the file was there and well-formed (`manifest.readable`), so the healthy value is never reached through the gap.

### Expected tables

Each family has a checked-in table: `configs/diagnostics/fixture_expected_facts.json` and `configs/diagnostics/behaviour_expected_facts.json`, and every live cell is read against `configs/diagnostics/live_invariants.json`.

```json
{
  "case_id": "behaviour",
  "requires": ["prep.stage_reached@work"],
  "facts": {
    "agent.worker_launched@work": {"expected": true, "compliance": true},
    "listing.complete@work": {"expected": true, "health": true},
    "workers.discovered_equals_listed@work": {"expected": true, "requires": ["agent.worker_launched@work", "listing.complete@work"]},
    "transcript.agent_steps_with_model_name": {"expected": "all", "by_harness": {"codex": {"expected": "none", "known_failure": {"issue": 898}}}}
  }
}
```

- `case_id` names the case the table grades; the live-invariants table has none and applies to any.
- An expected value is **exact** (`{"expected": v}`) or a **bound** (`{"at_least": n}`, `{"at_most": n}`, `{"contains": v}`).
- A fact with no entry in the table is recorded and never asserted.
- `"by_harness": {"<harness>": {...}}` overrides the matcher for one harness, for the facts whose truth differs by harness. An override that names no known failure inherits the entry's, so a table spells the healthy value at the entry and the defect, with its mark, in the override, never the other way round.
- `"known_failure": {"issue": N}` or `{"reason": "..."}` marks a fact known to fail, and its matcher then states the value the defect records, not the healthy one. A trial that records that value is `known`; one that records anything else is `failed`, because the defect changed or was fixed and the mark must come off. Every known failure is strict. An entry whose matcher is `{"expected": null}` must carry a known-failure mark: a fact declared unobservable is a defect with a name, never a permanent exemption.
- `"compliance": true` marks a fact that says whether the agent did what the prompt asked. `"health": true` marks a fact that says whether a compliance source could be read (the probe ran and parsed, every feed tool call's input was read, the listing was complete). A compliance or health fact that is null or not recorded is `failed`: the instrument could not read it, which is never the agent's doing.
- `"requires": ["<fact>", ...]` on a fact makes it conditional on compliance and health facts only: when one of them misses, the dependent fact is `precondition not met` instead of failed. A table-level `requires` may name any fact, and applies to every fact at once; every table requires `prep.stage_reached` throughout (the behaviour table on the `work` step), so a trial that never got through preparation reads as that one miss rather than as every fact failing. A requirement is met only when its fact passed.
- The table's validation refuses an unknown key, a requirement that is not a compliance or health fact, a cycle, and an `expected: null` with no known-failure mark.

A known failure and a requirement are declared in the table, never in code.
There is no per-pair override: a cell that cannot run on a pair is left out of the matrix (see [The cells](#the-cells)), never graded against an exception.

### Verdicts

- **not measured**: the infrastructure never produced a workspace: `preparation_stage` short of `created` with no seed-build failure recorded, or a harbor environment exception before the driver wrote state. No fact is asserted.
- **failed**: an instrument fact that is not a known failure missed, a known failure passed, a health fact missed, a compliance or health fact could not be read, or a fact the table asserts was not recorded.
- **not followed**: every miss is a compliance fact that read false, or a fact whose requirement missed. The agent did not do what the prompt asked, so the instrument facts that depend on it say nothing.
- **known**: every instrument miss is a declared known failure.
- **passed**: every fact matched, and no known failure is declared.

A failure anywhere after `created` (a seed that never came up, the tunnel, sign-in, the switch) is a failed `prep.stage_reached`, never "not measured": those stages are instrument.
An impossible seed is a failed `seed.build_status`, for the same reason: a fixture that no longer fits the template is the suite's own maintenance.
While any known failure is declared, the best verdict a night can have is `known`.

### Live invariants

A few facts are true of every trial whatever its case: `case.readable` (the step's `instruction.md` parses, which is what decides which records were due), `tools.every_call_has_one_result`, `steps.boundary_markers_match_case` (the markers name the case's steps in order), `transcript.source` is `"workspace"`, `harness.detected_is_lane_harness`, `manifest.readable`, and `workers.no_phantoms`, which requires the health fact `listing.complete`.
`live_invariants.json` declares them, requires `prep.stage_reached` throughout as the other tables do, and the `evaluate` job reads every live cell against it after `check-run`, in the same step. The report names a cell's misses once each, with the count of trials that missed, never per trial.
A miss is named in the report's details for the cell and gates nothing; it is the reading that turns "the readers are checked once a night on a fixture" into "the readers are checked on every real trial".

## The fixture family

One trial per pair, on the nightly's `haiku` config, whose app is fixed in the repo and whose flows are scripted, so every answer about the evidence is exact.

### Seeding: a seeded SHA, built before launch

**Decision: the driver builds the seeded commit in the box before the workspace exists, and creates the workspace from it.**
Building it in isolation finds a seed that cannot apply to the pair, a conflict or a collision, before any workspace is paid for, and ends that trial as impossible instead of running it against a half-seeded tree.
The workspace then boots with the seed registered, the way it boots with the template's own apps.

The suite needs one part of #935, **`seed.app`**, declared on a case's first step:

```json
"seed": {"app": {"source": "flow_lab_apps/todo", "name": "todo-fixture", "port": 8090}}
```

- `seed` is a key of a case's **first step**, because harbor uploads a `workdir/` and runs its `setup.sh` only for the steps of a multi-step task, so a seeded case is a stepped case, one step long if need be. Generation rejects `seed` on any later step.
- `source` is a directory resolved against the project root (`apps/minds_evals/`), under the same no-`..` rule `files` sources follow, so a config under `configs/` can reach `flow_lab_apps/` and the lab and the trial drive one file. The fixture directory holds `app.toml` and `icon.svg` beside the `index.html` the lab drives.
- `name` is the registry name and the program name; `port` is the loopback port the program binds, and generation rejects the template's reserved ports.

**The seed's shape.**
The seed commit adds `system/fixtures/<name>/`, holding the fixture directory verbatim, and a `[program:<name>]` block to `system/supervisord.conf` that registers the manifest through `forward_port.py --manifest` and serves the directory with `python3 -m http.server` on the port.
This is the build-app skill's escape hatch (a manifest, an icon, and a program wrapping an existing server) at a path the template's uv workspace globs do not cover, because a member under `system/apps/` needs a `pyproject.toml` and a relock.

**Building the seeded SHA.**

1. Generation copies `seed.app.source` into the first step's `workdir/`, and the step's `setup.sh` relocates it out of the box's working directory, as per-step `files` travel. Nothing goes into `environment/`, whose bytes key the image cache.
2. In `_prepare_workspace_clone`, after the eval-case commit, the driver commits the fixture and the program block on a throwaway detached worktree at `dwt_sha`, with the eval-case commit's fixed identity and dates: `seed_commit_sha`, the seed as a change to the template alone.
3. On the case clone's checked-out branch it runs `git merge --no-ff --no-edit <seed_commit_sha>`. Unmerged paths are a **conflict**, and `git merge --abort` restores the base.
4. It parses the merged `system/supervisord.conf` and refuses a second `[program:<name>]`, a registry name the template already registers, and a `--url` port another program already claims: a **collision**, and the branch is reset to the case base.
5. On a clean build the branch's head is the merge commit, `seeded_sha`, and the workspace is created from it; `dwt_sha` still names the template the pair pins.
6. Right after `create_workspace_and_wait`, before the tunnel or sign-in, the driver polls `supervisorctl status <name>` and `data/.state/apps.toml` until the program is `RUNNING` and its row exists, failing fast on `FATAL` or `BACKOFF`, within the preparation budget.
7. `state.json` records `seed_commit_sha`, `seeded_sha` and `seed.build_status` (`clean`, `conflict` or `collision`), and the collector receives `seeded_registrations = frozenset({name})`.

A conflict or a collision ends the trial before any workspace exists: the driver writes `state.json` with the build status and what collided, and raises `SeedBuildError`.
Harbor records the exception, still runs the verifier, and aborts later steps; `check-diagnostics` reads the verdict from `state.json`, never from the reward, and it is a **failed** verdict.
A merge is used rather than a plain commit onto the case base because the merge is the step that fails for #935's other seed sources, so one build path serves every source; both commits carry fixed identity and dates, so `seeded_sha` is a function of the case base and the seed alone.
`repo_state.json` records `seeded_sha` as the base the deliverable bundle is cut from, and counts the workspace's own first-boot commit (`Initial workspace commit`, authored by the bootstrap) apart from the agent's: it is the first commit beyond `seeded_sha` when its parent is `seeded_sha` and its author and subject are the bootstrap's.

Alternatives considered: a workspace-template branch shipping the fixture (changes `dwt_sha`, so the diagnostic would validate a different template from the night's live cells); landing the seed after create through `update_self.py apply` (product machinery, not instrument, and a restart of every workspace service on every nightly trial; see `update_path_notes.md`); the driver rsyncing files and running `supervisorctl` itself (a path no product change takes).

### Pre-existing, delivered, seeded

`resolve_delivered_apps` subtracts pre-existing, `internal` and abandoned-preview rows, and the seed is registered before the pre-turn-1 snapshot, so without a change its row would be pre-existing and no flow would have a target.

**Decision: a third class, not "count it as delivered".**
Counting a seeded row as delivered would make `min_registered_apps` and `app_registered` report an app the agent never built, which is exactly wrong for #935's motivating cases.

- `RegisteredApp` gains `is_seeded`, stamped by `parse_apps_registry` from a `seeded_registrations` argument, the way `is_preexisting` is stamped. The seeded set is declared by the config, since nothing observes the workspace before the seed is in it.
- `EvidenceCollector` gains `seeded_registrations` beside `preexisting_registrations`, and a `_probeable_apps` property: delivered plus seeded. HTTP checks and UI flows read the probeable set; the registration criterion keeps reading the delivered set.
- `EvidenceManifest` records `seeded_registrations` beside `preexisting_registrations`.

The fixture case declares `deliverable: {"kind": "minds-app", "min_registered_apps": 0}`, which registers the bundle, HTTP and app classes while the app criterion states the truth: the agent delivered nothing.

### Flow prerequisites

**Per-flow `start_path`.**
`start_path` on a flow is appended to the target origin, must be empty or start with `/` or `?`, and is how a flow reaches the fixture's knobs.

**Scripted flows.**
`UiFlow.script` is a list of actions in the executor's own vocabulary: `kind` (`click`, `input`, `keys`, `scroll`, `open`, `reload`, `wait`) and the `role`, `target`, `text` and `amount` that kind needs.
A `ScriptVerificationAgent` implements `VerificationAgent` with no model call: each entry becomes a `FlowAction`, then `done`, then a fixed reading.
The collector chooses the agent per flow, so only a model-driven flow uses the configured agent.
An element the page gives no accessible name is located with `beside` in place of `target`: the element of its `role` that the ARIA tree lists with no name, whose parent holds a line containing that text.
The agent resolves the locator against the page state the step is decided on and hands out a ref-addressed action, so the step script checks the ref against a fresh snapshot and the record carries `target_ref`, as for the model-driven agent's ref steps.
A locator that picks out no element, or several, is an unusable decision: the flow ends with status `error` and reason `verifier_agent_failed`, because a script belongs to the eval, so a locator that does not fit the page is an eval error, never the app's failure.

Scripted, every reading lands on a known step, and the flow lab confirms each scripted fact locally before a trial runs, because it executes the same step script and loop.

**A per-case judge screenshot cap.**
`render_flow_evidence.py` attaches each flow's last four frames to the outcome judge and caps the total at 24, dropping the earliest flows' frames first.
A case's `expectations` may set `max_judge_screenshots` (at most 100, the Messages API's image limit) in place of the default.
The fixture case does not need it: seven scripted flows offer 22 frames.

### The fixture case

`configs/eval-config-diagnostics-fixture.json`, one case of one step, on the nightly `haiku` config, with no proxy and no model-driven flow.

```json
{
  "mngr_branch": "main",
  "timeout_seconds": 1800,
  "verification_timeout_seconds": 1800,
  "personas": [{
    "id": "instrument",
    "persona": "A client who only wants an acknowledgement.",
    "steps": [{
      "name": "instrument",
      "seed": {"app": {"source": "flow_lab_apps/todo", "name": "todo-fixture", "port": 8090}},
      "prompts": [
        "Reply with the single word: acknowledged. Do not do anything else.",
        {"goal": "The assistant has replied with the word acknowledged.", "max_exchanges": 1}
      ],
      "expectations": {
        "outcome": "The workspace serves the seeded to-do fixture unchanged.",
        "deliverable": {
          "kind": "minds-app",
          "min_registered_apps": 0,
          "http": [
            {"target": "todo-fixture", "expect_status": 200, "expect_body_regex": "<title>Todo</title>"},
            {"target": "todo-fixture", "expect_status": 200, "expect_body_regex": "DIAG-absent-7f3a"}
          ],
          "files": [
            {"glob": "workspace/system/fixtures/todo-fixture/index.html", "min_count": 1},
            {"glob": "workspace/system/fixtures/todo-fixture/absent-7f3a.html", "min_count": 1}
          ]
        },
        "test_commands": [
          "test -s system/fixtures/todo-fixture/index.html",
          "test -e system/fixtures/todo-fixture/absent-7f3a.html"
        ],
        "timing": {"fast_seconds": 600, "slow_seconds": 1500, "requires_no_failures": ["http"]},
        "ui_flows": [
          {"name": "plain", "expect": "'walk dog' was added, completed and deleted; the seed tasks are untouched.",
           "script": [
             {"kind": "input", "role": "textbox", "target": "New task", "text": "walk dog"},
             {"kind": "click", "role": "button", "target": "Add"},
             {"kind": "click", "role": "checkbox", "target": "walk dog"},
             {"kind": "click", "role": "button", "target": "Delete \"walk dog\""},
             {"kind": "click", "role": "button", "target": "Refresh"},
             {"kind": "reload"}
           ]},
          {"name": "deferred", "start_path": "?latency=300", "expect": "'walk dog' is listed.",
           "script": [
             {"kind": "input", "role": "textbox", "target": "New task", "text": "walk dog"},
             {"kind": "click", "role": "button", "target": "Add"}
           ]},
          {"name": "armed-delete", "start_path": "?arm_delete=1", "expect": "'Buy milk' is gone.",
           "script": [
             {"kind": "click", "role": "button", "target": "Delete \"Buy milk\""},
             {"kind": "click", "role": "button", "target": "Delete \"Buy milk\""}
           ]},
          {"name": "pending", "start_path": "?pending=2000", "expect": "'walk dog' is listed once saving finishes.",
           "script": [
             {"kind": "input", "role": "textbox", "target": "New task", "text": "walk dog"},
             {"kind": "click", "role": "button", "target": "Add"},
             {"kind": "wait"}
           ]},
          {"name": "ticker", "start_path": "?ticker=1", "expect": "Nothing but the clock changes.",
           "script": [{"kind": "click", "role": "button", "target": "Refresh"}]},
          {"name": "start-over", "start_path": "?latency=300", "expect": "The page reloads at its own root.",
           "script": [{"kind": "click", "role": "link", "target": "Start over"}]},
          {"name": "unnamed", "start_path": "?unnamed=1",
           "expect": "'walk dog' was added, completed through its nameless checkbox and deleted; the seed tasks are untouched.",
           "script": [
             {"kind": "input", "role": "textbox", "target": "New task", "text": "walk dog"},
             {"kind": "click", "role": "button", "target": "Add"},
             {"kind": "click", "role": "checkbox", "beside": "walk dog"},
             {"kind": "click", "role": "button", "target": "Delete \"walk dog\""}
           ]}
        ]
      }
    }]
  }]
}
```

- The single literal turn keeps the agent's spend near zero while still exercising the turn loop, the welcome, the model switch and the transcript capture. The goal entry after it is already satisfied by that reply, so it exercises the goal-holding client and the entry records without another agent turn.
- **The second HTTP check, the second file glob and the second test command are meant to fail.** Each names a nonce that nothing in the workspace carries, so the manifest must record `failed` for it, the reader that decides pass from fail is exercised in both directions, and a reader that passes everything is red. Their effect on the fixture's reward is not a concern: reward is a non-goal, and `gates.results` pins whatever the structural gates make of them.
- Each flow runs in a fresh Chromium profile, so storage one flow leaves never reaches another.
- The fixture's seed rows (`Buy milk` unchecked, `Learn React` checked), its control names, its dead `Refresh` button, its knobs, its `Start over` link and its `?unnamed=1` page (every checkbox nameless) are the known answers.
- Flow evidence directories are slugified names: `plain`, `deferred`, `armed_delete`, `pending`, `ticker`, `start_over`, `unnamed`.

### Fixture facts

A flow's `log.jsonl` record for action k stores the page state from **before** that action; the state after action k is the next record's.
In an after-state, `checkboxes` is the list of `checkbox "<name>"` names in page order and `checked` the names among them marked `[checked]`, with the recorder's per-line suffixes ignored and a nameless checkbox read as the empty name.
`step.<k>.observed_kind` maps a record's `observed` onto the `ui_flows` summary constants by name, so a reworded constant does not red the table; `step.<k>.addressed_by_ref` is whether a record carries a `target_ref`.
Each flow is asserted on its status, its record shape and one reading that only its knob can produce; the rest of what the lab pins is recorded and never asserted here.

| fact | expected | notes |
|---|---|---|
| `prep.stage_reached` / `trial.completed` | `"conversation"` / `true` | |
| `gates.results` | the structural criteria `finalize.py` writes, each by name with its result | a gate deleted or renamed is a miss, whatever the others say |
| `manifest.readable` / `evidence.errored_entries` | `true` / `[]` | a failed entry is not an errored one |
| `evidence.statuses` | each entry's status by id: `passed` for the first HTTP check, glob and command, `failed` for the second of each | the negative checks |
| `seed.build_status` / `seed.in_head` | `"clean"` / `true` | |
| `repo.agent_commit_count` / `repo.bootstrap_commit_count` / `bundle.verified` | `0` / `1` / `true` | `git bundle verify` at check time; the bundle holds the first-boot commit and nothing of the agent's |
| `apps.seeded` / `apps.delivered` | `["todo-fixture"]` / `[]` | |
| `apps.service_state.todo-fixture` / `apps.registered_all_running` | `"RUNNING"` / `true` | every registered app's program is `RUNNING`, without naming the template's |
| `http.<entry id>.status` / `files.<entry id>.matched` / `test_commands.exit_codes` | `200` on each of the three HTTP entries / `1`, `0` / `[0, 1]` | keyed by the manifest's entry id (a check that fans out over several apps carries the target in its id); the kind's implied registered-apps fan-out is the third HTTP entry, and the negative HTTP check answers 200 too, since its body regex is what misses |
| `flow.<slug>.status` / `.record_kinds` / `.png_count` | `"passed"` / `["init", "action" x (N + 1), "final"]` / N + 1 | every flow; N is the script length |
| `flow.plain.after.4.checkboxes` / `.step.5.observed_kind` | `["Buy milk", "Learn React"]` / `"no_reaction"` | the delete; the dead `Refresh` |
| `flow.deferred.step.2.reaction` | `"settled"` | the render lands 300 ms after the click |
| `flow.armed_delete.step.1.observed_kind` | `"acknowledged_only"` | the first click only arms the button |
| `flow.pending.after.3.checkboxes` | `["Buy milk", "Learn React", "walk dog"]` | resolved by the `wait` |
| `flow.ticker.step.1.reaction` | `"still_changing"` | |
| `flow.start_over.after.1.url_query` | `""` | read once the new document has loaded |
| `flow.unnamed.step.3.addressed_by_ref` / `.after.3.checked` | `true` / `["", ""]` | the located checkbox, resolved and clicked |
| `judge.digest_flow_count` / `judge.screenshot_count` | `7` / `22` | check time, from `verifier/derived/` |
| `judge.unnamed_control_note` / `judge.addressed_by_ref_step_count` | `true` / `1` | the digest's note on unnamed controls and its ref-step marks |
| `transcript.source` / `transcript.user_step_count` | `"workspace"` / `1` | |
| `arm.harness_config.harness` / `.model_choice_switch` / `.is_model_confirmed` | `"claude"` / `"applied"` / `true` | the transcript path, as on every nightly claude cell |
| `snapshot.readable` | `true` | the last pull lists and contains `workspace/system/fixtures/todo-fixture/index.html` |
| `timing.turn_index` / `timing.seconds_within_elapsed` / `timing.agrees_with_feed` | `1` / `true` / `true` | the goal is satisfied by the reply to the first client message; the recorded seconds are positive and within the trial's `elapsed_seconds`; and the recorded span opens on the client message the feed stamps and closes at or after the reply it stamps, with a 30 s round-trip allowance at each end. The two records close on different events by design (the feed stamps the last agent message, the driver the poll that saw the agent waiting again; measured 1.5 s against 66.6 s on one trial), so their lengths are never compared; a span anchored on the welcome turn or the trial's start is minutes out and is what the fact catches |
| `evidence.statuses` gains the `time_to_goal` entry | `failed` | its `requires_no_failures` names the `http` class, whose negative check fails, so the timing check reads `failed` by construction (the `app` class passes on this case, since nothing is delivered and `min_registered_apps` is 0); the `timing` block's bounds are the trial's own preparation budget and timeout, so no fact decides on speed |

Recorded and never asserted: every other after-state and reaction, `flow.<slug>.reaction_counts` and `no_reaction_share`, `bundle.bytes`, `snapshot.bytes`, `entries.*`, `decider.call_count`.

**Measured.**
The first cut's fixture trial (mngr at its milestone-7 head, dwt `main@aecc8b84`, `haiku` with the proxy) matched every asserted fact of its larger table but two, both the workspace's own first-boot commit, which the bootstrap count now sets apart.
It cost $0.70 of agent spend, $0.08 of verification agent and under a cent of decider, in 18 minutes, most of the agent spend being the template's fast-mode greeting.
The revised case, run once live on 2026-09-15 (job `sd-r2b-fixture-0915`, mngr at the R2 branch head, 38 minutes, $0.24 of agent spend of which the template's fast-mode greeting $0.20, and $0.007 of decider), came out `passed` on all 61 facts of the table above: the three negative checks recorded `failed`, the gates by name were `agent_engaged_substantively`, `all_turns_completed`, `finished_within_time`, `progress_timeline_was_read` and `transcript_has_agent_reply`, and the seven flows offered 22 frames.
That run caught two defects of the instrument on its first pass: `git bundle verify` had never run on a relatively named bundle, and `cleanup-environments --job-dir` could not see a stepped trial's environment; both are fixed with the family.
Its trimmed job directory (280 KB) is checked in as the fixture test's input, so a wrong table fails the test.

## The behaviour family

One trial per harness of the nightly set, per pair, on a cheap model, whose agent is prompted to do precise things and whose facts are agreements between independent records.

### What it protects

Every nightly live cell is read by readers that differ by harness, and each has a way to be silently wrong:

- **Tool calls and progress.** The judged transcript and the failure scan read command output by tool name, and progress blocks are recovered from `tk`'s output lines. Codex wraps its shell in code mode, so both depend on unwrapping `_raw` programs (PR #936).
- **Errors.** The failure scan also reads any result marked `is_error`; mngr's codex converter marks none, so a failing codex command reaches the scan only through its text.
- **Models and usage.** Codex steps carry no `model_name` and no metrics (PR #898); a claude worker runs the template's pinned model whatever the lead was switched to (#1008).
- **Workers.** Launches are found by scanning command text, and a launch command that failed is still counted (#873).
- **Steps.** Boundary markers are placed by exact message or by timestamp and dropped with only a log line when neither matches; `entries_before` decides the turn gate; uploads must not exist before their step.
- **Harness detection.** A pi or codex trial whose document fell back to hand-built and whose accounts listing named no harness is graded as claude.
- **Skills.** A process expectation (`required_skills`, `forbidden_skills`, `max_worker_launches`) is read off the captured transcript by a reader that differs by harness: a `Skill` tool call on claude, a `skills/<name>/SKILL.md` path in what a call runs or opens on pi and codex (inside a codex `_raw` program too), and never a call whose result came back an error.

### The cells

- **Which harnesses.** The distinct harnesses of the `is_nightly` entries of `configs/harness_configs.json`, derived through `harness_for_lane`, which the trial cross-checks against the harness the workspace's accounts listing reports. Today that is claude, pi-coding and codex.
- **Which config.** Each harness names its cheap diagnostic config in `configs/diagnostics/behaviour_harness_configs.json`: `haiku` for claude, `openrouter/openai/gpt-5-mini` for pi-coding, and `gpt-5.6-luna` for codex, each with the flags a nightly cell of that lane gets and nothing more. A cell fetches only its lane's key, as a live cell does.
- **Which pairs.** Every pair the night evaluates, except where the config declares a pair it cannot run on (`"unsupported_pairs": ["released"]` on the codex entry, until a release carries the `openai` lane's pasted-key sign-in). `ci-matrix` leaves such a cell out and the report names it as unsupported; no box is spent to produce a dark cell, and no table carries a per-pair exception.

### The behaviour case

`configs/eval-config-diagnostics-behaviour.json`, one case of two steps, generated once per pair and run once per cell.
Both steps declare `diagnostic_probe: true`, which is what makes the collector take the probe the compliance facts read.

```json
{
  "mngr_branch": "main",
  "timeout_seconds": 4800,
  "verification_timeout_seconds": 900,
  "personas": [{
    "id": "behaviour",
    "persona": "An operator running a scripted harness self-check. They only want the exact steps carried out.",
    "steps": [
      {
        "name": "work",
        "diagnostic_probe": true,
        "prompts": ["This is an automated harness self-check. Carry out these items in order, exactly as written, one separate tool call per numbered item, and do nothing else. Do not use the Agent or Task tool, and create no ticket or step other than the ones below.\n1. Run: tk create --step \"DIAG alpha 7f3a\"\n2. Run: tk create --step \"DIAG beta 7f3a\"\n3. Run: tk start <the id tk printed for DIAG alpha 7f3a>\n4. Run: echo DIAG-7f3a-one\n5. Run: diag-missing-command-7f3a\n6. Run: tk close <the id of DIAG alpha 7f3a> \"alpha done 7f3a\"\n7. Run: tk create \"DIAG regular ticket 7f3a\" -t chore\n8. Run: tk start <the id of DIAG beta 7f3a>\n9. Run exactly this, as one command: mkdir -p data/.tasks/launch-task/diag-worker-7f3a && printf '%s\\n' '---' 'finish_report_path: data/.tasks/launch-task/diag-worker-7f3a/reports/report.md' '---' '' '# Task: diagnostic worker 7f3a' '' 'Write a finish report whose body is the single word ok, following .agents/shared/references/worker-reporting.md with name: done, then stop.' > data/.tasks/launch-task/diag-worker-7f3a/task.md\n10. Run exactly this, as one command: uv run .agents/skills/launch-task/scripts/create_worker.py launch --name diag-worker-7f3a --template worker --runtime-dir data/.tasks/launch-task/diag-worker-7f3a/ --task-file data/.tasks/launch-task/diag-worker-7f3a/task.md\n11. Run: tk close <the id of DIAG beta 7f3a> \"beta done 7f3a\"\n12. If you have a Skill tool, invoke the skill named build-app with it and stop as soon as it has loaded; otherwise run: cat .agents/skills/build-app/SKILL.md\nWhen all twelve items are done, reply with the single word: done."],
        "expectations": {
          "outcome": "The agent carried out the twelve items of the harness self-check, one tool call each.",
          "process": {
            "required_skills": ["build-app", "diag-never-invoked-7f3a"],
            "forbidden_skills": ["diag-forbidden-7f3a"],
            "max_worker_launches": 1
          }
        }
      },
      {
        "name": "check",
        "diagnostic_probe": true,
        "files": [{"source": "datasets/diag-upload", "upload_id": "diagupload7f3a"}],
        "prompts": ["Carry out these items in order, one separate tool call per item, and do nothing else. Do not use the Agent or Task tool.\n1. Run: cat data/uploads/diagupload7f3a/marker.txt\n2. Run: echo DIAG-7f3a-two\n3. Run: cat data/.tasks/launch-task/diag-worker-7f3a/reports/report.md\nThen reply with one short sentence saying whether the report says ok."]
      }
    ]
  }]
}
```

- **Exact commands, not skills.** Asked for a worker "with the launch-task skill", claude on haiku used the `Agent` tool and pi and codex improvised launch commands that failed; spelled out, all three launch the worker.
- **No clean-tree step.** The task file and the reports live under the gitignored `data/.tasks/`, so the launch's clean-tree check passes without a commit.
- **Every literal the facts look for carries the nonce `7f3a`**, so no template text can satisfy a fact by accident, and compliance facts match tickets by their nonce titles, never by count, because a harness's own reminders create step tickets the prompt did not ask for.
- **No `min_reward` on step 1.** A floor there would abort step 2 exactly when the progress reader regressed. A step that never ran is a failed `trial.completed`.
- **Step 2's upload** does not exist during step 1, and its marker is read back, so the upload hop and the step-local evidence are both checkable.
- **Item 12 is one literal for two readers.** On claude the skill is a `Skill` tool call; on pi and codex, which have no such tool, it is a read of the skill's file, which is what their reader looks for. The `process` block on the step declares the skill as required beside a nonce skill nothing invokes and a forbidden nonce nothing reads, so the process entries record `passed`, `failed` and `passed` by construction, and `max_worker_launches` is the one launch item 10 makes. A process block is the only expectation the step declares, so the collector's evidence phase runs for it alone.

### The records compared

| source | what it is | taken |
|---|---|---|
| **T** tickets | `verification/tickets.jsonl`, from `data/.tickets/*.md` | collection, per step |
| **E** event feed | the chat app's event feed, whose tool results carry a `tk_stamp`, and each tool call's input from the per-event detail endpoint; both in `driver_events.jsonl` | polled by the driver; the details read in one exec on a probe step |
| **A** trajectory | the captured ATIF document, `trajectory.json` | collection, per step, cumulative |
| **R** derived | `verifier/derived/progress_summary.json`, `harness_failures.json`, `judge_transcript.txt`, and `reward-details.json`'s `harness` block | check time |
| **W** listing | `verification/workers/agents.json` with `listing.json`, taken unconditionally | collection, per step |
| **D** diagnostic probe | one exec at collection on a probe step, printing raw `ls` and `cat` of `data/.tickets/`, `mngr list --format json` names, and whether the worker's report exists; parsed by `diagnostic_probe.py`, which shares nothing with the collectors | collection, per step |
| **S** step records | `state.json`, `usage.json`, `result.json`, `verification/manifest.json`, `driver.log` | per step |

Compliance facts read only E and D.
The parsers are independent of the readers under test; the sources are not entirely: D and W both run `mngr list`, and E's details come from one exec.
So each compliance source has a **health fact** beside it, and a health miss is `failed`: a listing regression or a detail read that did not answer is the instrument's failure, never "the agent did not comply".

### Behaviour facts

**Compliance facts** say whether the agent did what it was asked.

| compliance fact | expected | source |
|---|---|---|
| `agent.step_tickets@work` / `agent.regular_ticket@work` | step tickets titled `DIAG alpha 7f3a` and `DIAG beta 7f3a` exist and are closed; a non-step ticket titled `DIAG regular ticket 7f3a` exists | D |
| `agent.ran_nonce_echo@work` / `agent.ran_missing_command@work` | E holds a tool call whose input contains `echo DIAG-7f3a-one` / `diag-missing-command-7f3a` | E |
| `agent.worker_launched@work` / `agent.worker_finished@check` | the listing D printed names `diag-worker-7f3a`; the worker's report exists at step 2 | D |
| `agent.read_upload_marker@check` | E holds a tool call whose input names the upload's marker path | E |
| `agent.invoked_skill@work` | E holds a `Skill` call naming `build-app`, or a tool call whose input reads `.agents/skills/build-app/SKILL.md` | E |

**Health facts** say whether those sources could be read.

| health fact | expected |
|---|---|
| `probe.read@work` / `probe.read@check` | the probe ran and its output parsed |
| `feed.inputs_read@work` / `feed.inputs_read@check` | every tool call in the feed has its input read (`feed.tool_calls_without_input` is `0`) |
| `listing.complete@work` / `listing.complete@check` | the listing exited zero and reported no errors |

**Instrument facts**, grouped by the reader they check; each requires the compliance and health facts its reading depends on.

| fact | expected | requires |
|---|---|---|
| `progress.step_ids_agree` | the nonce-titled step ids in T equal those in A's executing-tool output and those in E's `tk_stamp` | `agent.step_tickets` |
| `progress.declared_titles_agree` / `progress.done_summaries_agree` / `progress.nonce_block_count` | each nonce-titled T title and closed summary equals exactly one rendered progress block; `4` nonce-titled blocks in R | `agent.step_tickets` |
| `progress.regular_ticket_rendered` | `false`: a non-step ticket yields no block | `agent.regular_ticket` |
| `tools.every_call_has_one_result` | `true` in A | |
| `tools.nonce_in_one_executing_result` | `DIAG-7f3a-one` appears in exactly one result R's executing-tool list reads | `agent.ran_nonce_echo` |
| `failures.missing_command_count` / `failures.missing_command_is_error` | `{"at_least": 1}` in R's main counts; `true`, codex `false` as a known failure | `agent.ran_missing_command` |
| `workers.no_phantoms` | every discovered worker is listed, or destroyed with its stream preserved | `listing.complete` |
| `workers.listed_agent_created` / `.discovered_equals_listed` / `.captured_equals_listed` / `.embedded_equals_listed` | `["diag-worker-7f3a"]`; the discovered, captured and embedded names each equal the listing's | `agent.worker_launched`, `listing.complete` |
| `workers.launch_count@check` / `.captured_count@check` / `.report_captured@check` | `1` / `1` / `true` | `agent.worker_finished` |
| `workers.harness_is_lead_harness` | the worker's listed type is the lead's harness | `agent.worker_launched`, `listing.complete` |
| `workers.model_is_lead_model@check` | the captured worker's steps name the model the lead was switched to; claude and pi-coding `false` as known failures (#1008: a worker runs the template's default for its lane, `opus[1m]` on claude and `moonshotai/kimi-k2.6` on pi, whatever the lead was switched to), codex `null` as a known failure (#898) | `agent.worker_finished` |
| `steps.boundary_markers@work` / `@check` / `steps.boundary_markers_followed_by_opening` | `["work"]` / `["work", "check"]` / `true` | |
| `entries.count@work` / `@check` / `steps.entries_before@work` / `@check` | `1` / `2` / `0` / `1` | |
| `steps.upload_marker_present@work` / `@check` / `steps.upload_marker_read@check` | `false` / `true` / `true` | the last requires `agent.read_upload_marker`; the marker's text must appear in a tool result of step 2 whatever tool read it, since the prompt asks for a read and pi's `read` tool is not a shell |
| `steps.driver_log_step_local@check` / `steps.trajectory_cumulative@check` / `steps.spend_deltas_sum@check` | `true`; codex `null` on the last as a known failure (#898: harbor records no input tokens and all-zero workspace tokens for a codex step, so the deltas cannot be summed) | |
| `transcript.source` / `transcript.agent_steps_with_model_name` | `"workspace"` on every step; `"all"`, codex `"none"` as a known failure (#898) | |
| `harness.detected_is_lane_harness` | `true` on every step | |
| `arm.harness_config.is_model_confirmed` | `true`; codex `null` as a known failure (#898: no model on codex steps) | |
| `usage.tokens_present@check` / `usage.is_cost_complete@check` | `true`, codex `false` as a known failure; `true` | the last requires `agent.worker_finished` |
| `process.invoked_skills@work` | `["build-app"]`, as the transcript reader recorded them | `agent.invoked_skill` |
| `process.worker_launch_count@work` | `1` | `agent.worker_launched` |
| `evidence.statuses@work` | the process entries: the required `build-app` `passed`, the required nonce `failed`, the forbidden nonce `passed`, the launch count `passed`; and the always-on `file_inventory` entry `passed`, which every step's manifest carries whatever the case declares | `agent.invoked_skill`, `agent.worker_launched` |

Dollar figures are recorded, never asserted: a greeting on an unpriced default model (pi on OpenRouter greets on `moonshotai/kimi-k2.6`) leaves the trial's cost `null` on a perfectly healthy trial.

**Measured.**
The first cut's cells (mngr `f2632ae2`, dwt `main@aecc8b84`) came out `passed` on claude `haiku` (25 min, $1.30, of which the template's fast-mode greeting $0.65 and the `opus[1m]` worker $0.47), `known` on codex `gpt-5.6-luna` for exactly its declared defects (28 min, unmetered), and `passed` on pi `gpt-5-mini` (38 min, cents plus the unpriced greeting).
The revised case with item 12, run live on 2026-09-16 (mngr `main@c1e4357690`, dwt `main@d525d62ef9`), came out `known` on every cell with exactly its declared facts and nothing `not followed`: claude `haiku` 37 min and $0.84, pi `gpt-5-mini` 26 min and $0.03 plus the unpriced greeting, codex `gpt-5.6-luna` 36 min unmetered. All three invoked the skill, claude through its `Skill` tool and the others by reading the skill's file, and the codex cell surfaced `steps.spend_deltas_sum` as a further #898 defect that the earlier hand-built fixtures had passed vacuously. The three trimmed job directories are checked in as the behaviour tests' inputs.
Read against the revised table, the first cut's records come out `known` on every cell too: the worker-model fact (#1008) holds on claude and on pi, whose worker ran `moonshotai/kimi-k2.6` while the lead was switched to `gpt-5-mini`, so no cell can read `passed` until #1008 is fixed and its marks come off.
Whether a gpt-5-mini worker reports is a property of the model: one round it did not, and `agent.worker_finished` is a compliance fact so that cell reads `not followed`, never `failed`.
PR #937 carries the full measured tables of the first cut.

## The verification agent

The model-driven UI-flow agent is not exercised by this suite: a flow it drives can only add red for reasons that are not the instrument's.
Its mechanics (well-formed actions, refs only where the page gives no name, `done`, no unusable decision on a page it can address) are #1019's, whose natural home is the flow lab as release tests with a real cheap model, run by a small scheduled job of their own so the assessment is nightly even though release tests are not in PR CI.
The agent runs on the host in both the lab and a trial, so the lab exercises the same code and step script; only the box transport differs, which the fixture's scripted flows cover.
Its judgement on real apps is recorded, never gated, from the live cells' flows, and the report can aggregate `verifier_agent_failed`, unusable decisions, no-reaction share and ref-step share once the live invariants land.
If an in-box agent-driven flow is ever wanted in the fixture, its facts are declared `compliance: true`, so a flub reads `not followed`.

## CI integration

Two jobs in `.github/workflows/minds-evals-scheduled.yml`, beside `evaluate`: `diagnose-fixture`, one entry per pair, and `diagnose-behaviour`, one entry per pair and supported harness.

- **Deciding them.** `minds-evals ci-matrix` emits `diagnose_fixture_matrix` and `diagnose_behaviour_matrix`, whatever the green markers say, each carrying the pair's SHAs and the `--ak` flags built from the family's harness config through the same parsing `harness_config_kwargs` applies. An unsupported pair and harness is left out and listed in a `diagnose_unsupported` output for the report.
- **Gating them.** `needs: [resolve]` and `if: ${{ !cancelled() && needs.resolve.result == 'success' && needs.resolve.outputs.is_live_pass_skipped != 'true' && needs.resolve.outputs.is_any_pair_diagnosed == 'true' }}`; the last clause exists because GitHub refuses an empty matrix. They are not gated on the oracle, which is skipped on an all-green night.
- **Never skipped by a marker.** The diagnostics write no marker and read none. Oracle-only runs skip them, since they are live passes.
- **In parallel with `evaluate`, gating nothing.** Promote them once they have been green for a few weeks.
- **Steps**, mirroring `evaluate`'s: the Vault fetches, the throwaway Modal profile, the `ci-user-id-prefix` step, `generate` at the pair's frozen SHAs, `just minds-evals-run`, `check-diagnostics` before the job-directory upload, `cleanup-environments --job-dir`, and, in `diagnose-fixture` only, the age-based backstop sweep. `timeout-minutes` is 200.
- **Names**: job titles `diagnose fixture (<pair>)` and `diagnose behaviour (<pair> x <harness>)`, concurrency groups `minds-evals-diagnose-fixture-<pair>` and `minds-evals-diagnose-behaviour-<pair>-<harness>`, harbor job names `<pair>-diagnose-fixture-<run_id>` and `<pair>-diagnose-behaviour-<harness>-<run_id>`, and summary artifacts as `evaluate`'s with `diagnose-fixture` and `diagnose-behaviour-<harness>` in the config position. `ci_matrix` refuses a harness config named `diagnose` or starting with `diagnose-`, since a live cell's summary artifact would collide with these.
- **Job result.** `failed` fails the job; `known`, `not followed` and `not measured` do not.
- **Live invariants.** `evaluate` runs `check-diagnostics --expected configs/diagnostics/live_invariants.json` beside `check-run`, and its misses go into the summary as details, never into the job result.
- **Reporting.** `notify` needs both jobs, and `ci-report` reads every diagnose summary of a pair whatever the pair's decision. The pair's opening line gains the worst diagnostics verdict, with the families and harnesses named; a `failed` diagnostic makes the pair `failed`; `known`, `not followed`, `not measured` and unsupported cells are named in the details, so a night without a measurement is never read as a clean one; failing facts join the failed-trials table, one row per fact. Two consecutive nights of `not measured` or `not followed` on the same cell are named as **coverage lost** in the opening line (see [Open questions](#open-questions)).
- **Issue filing** belongs to #793. A `failed` diagnostic is never the agent's fault, so it arrives pre-classified: `failed_facts` is the signature, and the pair, family, harness and sorted fact names are the dedup key.

## Cost

Per night, per pair: one fixture trial and three behaviour cells, each 20 to 40 minutes on its own box, and about $2 of model spend for the pair on the measured configs; the template's fast-mode greeting is the largest single item on every claude cell.
Box time is not counted: the README carries no Modal rate.

## Milestones

The first cut landed milestones 1 to 3 on this branch: per-flow `start_path`, scripted flows on the production class, and the producers that persist structured evidence (exit codes, HTTP status codes, matched counts, the seed record, `preparation_stage`, the tickets capture, the unconditional listing, the verifier's derived outputs) together with a fact block the driver wrote into `state.json`.
The revision keeps the producers and replaces the block with check-time computation; the branches below are rebuilt on this one, each a draft PR chained on the one before and merged down into it once reviewed.

1. **Check-time facts and the checker.** `evidence_facts.py` moves to the checker and reads files; the driver's fact block, its `evidence_facts` and `evidence_facts_error` state keys and the README section describing them go, replaced by a "Checking a diagnostic run" section; the producer additions under [The persisted record](#the-persisted-record); `check-diagnostics` with `case_id`, the matchers, `by_harness`, strict known failures, compliance and health facts, `requires`, the `not recorded` status, the five verdicts and `--record-only`. Proves assertion against recorded job directories, including past live ones.
2. **The fixture family.** Seeding, the seeded class, `beside` and ref-addressed scripted actions, the judge screenshot cap, the revised fixture case with its negative checks, and the table. Its test runs the checker over a trimmed job directory captured from a live run, so a wrong table fails the test. Proves the family in one live run per pair.
3. **The behaviour family.** `harness_for_lane`, the cells with `unsupported_pairs`, the case, the upload dataset, the feed-detail read, the diagnostic probe, the compliance, health and instrument facts, and the table with its known failures. Its tests run the checker over the captured trajectories and feeds of each harness. Proves the family in one live run per harness.
4. **CI.** Both matrix outputs, both jobs, the unsupported output, the `ci-report` changes, and the live-invariants read in `evaluate`. Proves the suite runs nightly.

Follow-ups, not built here: the verification agent's scheduled lab job (#1019); proxy calibration and stray-agent detection as a further family, once the proxy works on every harness (`proxy_calibration_notes.md`); mid-trial changes through the update path (`update_path_notes.md`); the aggregation of recorded flow facts across live cells in the report; a case-level switch that skips the outcome judge's model call on a diagnostic case while keeping the renderers whose outputs the `judge.*` facts read, since the judge's score of a control case means nothing and costs about $0.50 per fixture trial (it needs a look at whether rewardkit runs the renderers without the call).

## Open questions

Each carries the answer this spec assumes.

1. **Should the diagnostics gate the live cells?** *No, for now.* Revisit after a few weeks green.
2. **Should a green marker ever skip them?** *Never.*
3. **When does a cell that is never measured or never followed turn red?** *Named every night it happens, and named as coverage lost on the second consecutive night, in v1.* Making coverage lost fail the pair is the follow-up after a few weeks of nights; a harness whose cheap model is `not followed` night after night has lost its protection, and the answer is a better model for that cell rather than a looser table.
4. **Does a behaviour cell run on the `released` pair?** *Yes, like the live cells, where the lane can sign in.* Where it cannot, the cell is unsupported and named, not run.
5. **Should a flat case accept `seed`?** *Not in v1.* Harbor carries per-task files into the box only through a step's `workdir/`.
6. **Should the seed build move to generation?** *Later.* Generation holds no checkout of the template today.
7. **Should the seeded rows be measured rather than declared?** *Not while the seed is part of the boot tree.*
8. **Do workers share the lead's `.tickets`?** *Yes.* `TICKETS_DIR` is workspace-wide, so the compliance and progress facts match on the nonce titles.

## What the suite still does not catch

- **Harnesses and models outside the nightly set.** A harness no nightly config drives gets no behaviour cell, and a cheap model's output shapes are not necessarily the nightly model's; in particular the nightly `default` config, which requests no switch, has no diagnostic cell.
- **What only the proxy sees.** A request the transcript does not account for (a stray agent, an ancillary model) is invisible without the proxy; that is the later family's job.
- **Stale refs.** Only the flow lab's tests exercise the step script's `stale_ref` refusal; the nightly fixture drives no stale ref.
- **A control a script cannot single out.** A `beside` locator that matches zero or several elements is an eval error, never the app's failure. A per-action strictness flag is deferred until a case needs it.
- **The verification agent's judgement**, and **the judge's**: reported, never gated.
- **The evidence-check surface on one harness.** Flows, HTTP checks, files and commands run only on the fixture, on the claude lane; the behaviour case declares no expectations.
- **The template's update path**, **bundle creation with an agent's content**, **the `minds-ui` surface**, and **Claude Code's own subagents**: none has a case here.
- **Whatever the fixture does not do.** One small static page exercises no server round trip, login or second page.
- **Timing thresholds.** The fixture asserts the timing record and its agreement with the feed, never whether a turn was fast or slow; a `time_to_goal` criterion that scores a healthy trial wrongly against its bounds is a judgement the suite does not make.
