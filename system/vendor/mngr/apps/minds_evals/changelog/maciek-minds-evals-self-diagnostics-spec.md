The minds_evals self-diagnostic suite (#934) validates the eval instrument rather than the agent: a fixture family and a behaviour family run beside the live cells every night, and `minds-evals check-diagnostics` asserts expected-fact tables against the job directory harbor collected. The design is `specs/minds-evals-self-diagnostics/spec.md`.

A UI flow can open somewhere other than its app's root.

- A `ui_flows` entry takes an optional `start_path`, joined onto the app's forwarded origin for that flow's opening navigation: `?latency=300` opens `https://<label>.<agent>.localhost:<port>/?latency=300`, and `/tasks` opens that path. Empty, the default, opens the root. Each flow in a case carries its own, so one case can drive a fixture under several of its query-string behaviours.

- Generation rejects a `start_path` that is not empty and does not begin with `/` or `?`, one that begins with `//`, and one holding anything but printable ASCII without spaces or backslashes, so a flow cannot be pointed at another origin. The driver applies the same rule when it reads a case's flows back.

- `minds-evals flow-lab` takes the same value as `--start-path` (in place of `--page`), under the same rule, and joins it onto the served origin the way a trial joins it onto the forwarded one.

A UI flow can be scripted, so it runs the same actions every time and makes no model call.

- A `ui_flows` entry takes a `script` in place of `actions`: a list of actions in the executor's own vocabulary, each a `kind` (`click`, `input`, `keys`, `scroll`, `open`, `reload` or `wait`) plus the `role`, `target`, `text` or `amount` that kind needs. A scripted flow still carries an `expect`.

- Generation rejects an unknown kind or key, a `done` (every script ends with one on its own), a field the kind needs but lacks or does not take, an `open` whose text is not a start path on the app's origin, an empty script, and a script of more than 14 actions (the closing `done` takes the last of a flow's 15 steps). The driver applies the same per-action rules when it reads a case's flows back.

- A scripted flow's actions are performed exactly as written, each recorded with the reasoning `scripted`, followed by `done` and the fixed reading `scripted flow; no reading`. Its script is rendered into the flow's `actions` as numbered prose, so the judge's digest and the flow log read it like any other flow.

- The verification agent drives only the flows without a script. A trial with no key for it records those as `verifier_agent_failed` and still runs its scripted flows, whose decisions add nothing to `verifier_agent_usage`.

- `minds-evals flow-lab` takes `--script <file>`, a JSON file holding a flow's script, in place of `--actions`; a scripted run needs no `ANTHROPIC_API_KEY`.

Trials record the readings the self-diagnostic suite's checks are computed from. Each one is a record a finished trial now carries, rather than something re-derived from prose or lost inside a container.

- **`state.json` says how far preparation got.** `preparation_stage` names the last stage the trial completed: `created`, `proxied` (proxied trials only), `signed_in`, `chat_created`, `welcomed`, `switched` (only when the harness config names a model), then `conversation` once turn 1 is sent. It is empty before the workspace exists. A trial whose clone preparation or workspace create raises is marked `timed_out`, with a `timed_out_reason` naming the error, and writes timeout diagnostics before the error propagates.

- **The verifier keeps what it derived.** On its way out, whatever happened before, `test.sh` copies `progress_summary.json`, `harness_failures.json` and `judge_flows_digest.txt` into `/logs/verifier/derived/`, along with `judge_screenshots.txt`, which names the screenshots the judge was given. harbor collects that directory into the trial's `verifier/`. A failed copy never changes the grade.

- **The workspace's tickets are captured.** Every trial with a workspace writes `verification/tickets.jsonl`, one `{id, type, status, is_step, agent, title, summary, created, closed}` record per `data/.tickets/*.md` file. It takes one exec, is bounded in size, and adds no manifest entry. A capture that fails writes the reason instead of the records, and a ticket file with no frontmatter is left out and named.

- **The agent listing is taken on every trial.** `verification/workers/agents.json` is written whether or not the command scan found a worker launch. Each listed agent keeps its mngr labels (`agent_created` included), and the listing's `errors` are kept, so a partial listing is not read as a complete one.

- **Failed proxy requests are logged.** `usage_proxy.jsonl` gains one `"outcome": "failed"` record per request the proxy failed, carrying the model, `status_code`, `error_class` and zero tokens. Successful records carry `"outcome": "succeeded"`, and a record with no `outcome` is read as a success. Usage summaries leave failures out of tokens, cost and message counts and report them as `failed_request_count`.

- **Screenshot and snapshot sizes are recorded.** Every flow log `init` and `action` record carries `screenshot_byte_count` and `is_screenshot_png`, read back from the frame the step script wrote. The driver also keeps the byte count of the last workspace snapshot it pulled, which is 0 when the pull failed and null when it pulled none.

- **Test commands and files checks carry structured readings.** A `test_command` manifest entry carries its `exit_code`. Each declared files check gets its own manifest entry with the glob's `matched_count`, counted inside the workspace over the same inventory entries and with the same matching the verifier uses. It is `failed` below `min_count` and `error` when the inventory itself was not captured. The verifier's files score is computed exactly as before.

- **The collector hands back what it read.** After collection the driver holds the step's manifest, the services and supervisord text, each flow's run (with the calls that flow made to its verification agent), the worker captures, the agent listing, whether that listing is complete (read, exit 0, no errors) and the ticket capture.

- `file_inventory_command` takes the staging directory it writes the inventory into; the collector passes the workspace's `/tmp/minds-evals-verification`, and its test passes a directory of its own.

- Tests that use the `chromium_path` fixture are marked `chromium` automatically, and `just test-minds-evals` runs them in a pytest session of their own, apart from the rest of the suite, both across two xdist workers; each session stays under the 150 s CI limit and the coverage report covers both.

- The self-diagnostic suite's facts are computed at check time, by `minds-evals check-diagnostics`, from the records a finished trial persists into its job directory. No `state.json` carries a fact block.

- A trial's worker records say whether every discovered worker is listed or was destroyed with its stream preserved, and whether the agent listing was complete (read, a zero exit, no errors) -- since a partial listing cannot say whether an unlisted worker was destroyed.

- Each HTTP probe's manifest entry carries the observed `status_code` beside its prose.

The self-diagnostic suite asserts at check time, from the job directory harbor collected, so the driver computes nothing for it and persists only records. `state.json` carries no `evidence_facts` block.

- **`state.json` carries three more readings of the run**, beside `entries`: `snapshot_byte_count` (the last workspace snapshot's size, 0 when the pull failed and `null` on a trial that pulled none), `decider_call_count` (the calls made to the decider model, which a goal entry pushes past the message count) and `client_messages` (every message the driver sent as the client, in order, blank ones left out).

- **The evidence bundle keeps the texts the delivered-app set was resolved from.** `verification/supervisord.conf` and `verification/isolated_instance_services.txt` are captured verbatim beside `services.txt`, and the manifest's `is_registry_present` says whether the app registry could be read at all -- which an empty `apps.toml` cannot, since an unreadable registry and a workspace that registered nothing both leave it empty.

- **The agent listing says how it itself went.** `verification/workers/listing.json` records the listing command's `exit_code` (`null` when the bridge never ran it), its `errors` and `is_complete`, true only for a listing that ran, exited 0 and reported no errors -- since only a complete listing can call a worker it does not name destroyed.

- **Every worker the capture handled is recorded.** `verification/workers/captures.json` holds one object per capture (the worker's name, ids, state, depth, lead, directory under the bundle, and whether each part came out), then one per launch the caps left uncaptured, marked `is_overflow`. It is written on every trial with a workspace, the empty list included.

- **A failed tickets capture says so on disk.** `verification/tickets.jsonl` then holds a single `{"failure_reason": "<why>"}` record instead of being absent, so an unread ticket directory is not read as a workspace with no tickets.

- **Each finished UI flow records its outcome beside its log**, as `verification/flows/<slug>/run.json`: the flow's `status`, the `reason` it did not complete, and the calls that flow alone made to its verification agent. Its own file rather than a last log line, because the judge's renderer reads every log record as a flow step.

- **Each step-boundary marker in the trajectory carries the step's opening message**, under `extra.minds_evals.opening_message`, so the marker's placement is checkable against the conversation from the document alone.

- **Every `driver_events.jsonl` record names its kind** under `type`: a polled event is `feed_event` and keeps the workspace's own event verbatim under `event`, and a decider call is `decider_message`.

The self-diagnostic checker reads a finished job directory and asserts what it recorded. It is the other half of the records the producers now persist.

- **`minds-evals check-diagnostics <job_dir>` computes every fact itself**, from the job directory harbor collected, and gives each trial one verdict. Each step of each trial yields one block of dotted fact names, read from the same records the graders read, so nothing has to be computed while a trial runs.

- **A fact reports one of three things, and a table tells them apart.** A record that was never due on the step (no such check declared, no snapshot pulled, no earlier step to be step-local against) leaves its fact *omitted*; a record that was due on a step that ran and is absent or unreadable is *not recorded*, which fails any fact asserted on it; a record that is there and cannot answer is *null*, and matches only `expected: null`. `case.readable` and `manifest.readable` are the two facts that always answer, since they say whether the records that decide what everything else was due could be read at all.

- **Expected-facts tables assert facts by reference.** A table names the `case_id` it grades (none applies it to any case, and a job whose trials ran another case is refused), and its keys are `<fact>` for the last step that ran or `<fact>@<step>` to pin one. Each entry gives exactly one matcher -- `expected`, `at_least`, `at_most` or `contains` -- plus optional `by_harness` overrides, a `known_failure`, a `compliance` or `health` mark, and the compliance and health facts it `requires`.

- **A known failure names an issue (`{"issue": N}`) or a reason, and is always strict.** Its matcher states the value the defect records, so a trial that records it is `known` and a trial that no longer does is a failure saying the mark must come off. A `compliance` fact says whether the agent did what the prompt asked and a `health` fact whether a compliance source could be read at all; either one that reads null or was not recorded fails, since the instrument could not read it.

- **Five verdicts, in precedence: not measured, failed, not followed, known, passed.** Only `failed` exits non-zero. The summaries carry every fact that decided a trial, each with its status -- `passed`, `failed`, `not_followed`, `precondition_not_met`, `not_recorded`, `known` or `unexpectedly_passing`.

- **`--record-only` grades nothing and writes the facts out.** Either way, the computed blocks land as `<summary stem>-facts.json` beside whichever summary path was given, one entry per trial and step, so a run's full reading is available whether or not a table asserted on it. A table carries no per-pair exception: a cell that cannot run on a pair is left out of the matrix instead.

- **`configs/diagnostics/live_invariants.json` holds the facts true of every trial whatever its case**: one tool result per tool call, step-boundary markers that match the case, a transcript from the workspace's own document, the verifier's detected harness matching the lane's, a readable manifest, a complete agent listing, and no phantom workers.

- A stepped case's first step can declare `seed: {"app": {"source", "name", "port"}}` to boot its workspace with a known app. `source` is a directory under the minds_evals project root holding an `app.toml` and its icon; generation rejects a seed on a later step, on a flat case or at case level, an unknown key, an absolute or `..` source, a missing source or manifest, a name the workspace template's app-name rule refuses, and the template's reserved ports. The app travels in the step's `workdir/` and its `setup.sh` moves it out of the box's working directory.

- Before the workspace is created, the driver commits the app to `system/fixtures/<name>/` with a supervisord program on the pinned template, merges that commit onto the case clone's branch with fixed identity and dates, and checks the merged supervisord config for a second program of the same name, a registry name or a port another program already claims. The workspace is created from the resulting seeded SHA, and the deliverable bundle is cut from it. After create, the driver waits until the seeded program is `RUNNING` and its registry row exists.

- A seed that conflicts with the case base or collides with the template ends the trial before any workspace is created: `state.json` marks it timed out and its `seed` record carries `build_status` (`conflict` or `collision`), the paths or collisions, and an `impossible_reason`, and the step raises `SeedBuildError`. A seeded app that never comes up marks the trial timed out naming it.

- `state.json` carries the `seed` record and the `seed_built` and `seed_running` preparation stages, and `repo_state.json` carries `seed_commit_sha`, `seeded_sha` and the bundle's byte count.

- The flow lab's `todo` fixture gains an `app.toml` (`todo-fixture`) and an `icon.svg`, so it can be seeded as it is.

- A seeded app is its own registry class beside pre-existing and delivered. The collector aims the HTTP probes (the `registered-apps` fan-out and a probe naming the app) and the UI flows at it, but it never counts towards `min_registered_apps` or a service check, so a seeded trial where the agent built nothing scores exactly that. A case that expects nothing delivered declares `min_registered_apps: 0`.

- A seeded trial whose pre-existing set or registry could not be read stays unmeasured (`preexisting_unknown`, `registry_absent`, `registry_unreadable`), as an unseeded one does.

- `manifest.json` carries `seeded_registrations` (sorted names, empty on an unseeded trial) beside `preexisting_registrations`, and `apps.toml` rows the case seeded are excluded from the delivered set.

- `repo_state.json` counts the workspace's own first-boot commit (`Initial workspace commit`, authored by the template's bootstrap) apart from the agent's: `bootstrap_commit_count` is `1` when the first commit beyond the workspace's starting commit is that one, and `agent_commit_count` is every other commit beyond it. The deliverable bundle still carries the bootstrap commit, which a replay needs to unbundle onto the regenerated base.

- A scripted UI flow can act on an element the page gives no accessible name. A `click` or `input` names `beside` in place of `target`, and acts on the element of its `role` that has no name and whose parent element holds a line containing that text. The ref is read off the page state the step is decided on, so the step is checked and recorded like any step addressed by ref, `target_ref` included. Generation rejects `beside` together with `target`, and on an action that addresses no element.

- A locator that picks out no element, or several, ends the flow as `verifier_agent_failed`, with a `(no usable action)` step that says what it found. It is an eval error, never the app's failure, and no per-action setting changes that.

- A case's `expectations` take `max_judge_screenshots`, an integer from 1 to 100, which bounds how many flow screenshots the outcome judge is shown in all in place of the default 24. A case whose flows ask for more frames sets it, so the earliest flows' frames are not the ones dropped.

- A UI flow entry in a case config may carry notes for its reader under keys starting with `_comment`, which generation ignores.

- `configs/eval-config-diagnostics-fixture.json` is the self-diagnostic fixture case: one step that seeds the flow lab's to-do fixture, asks the agent for a single literal word, and drives seven scripted flows against the seeded app. Its second HTTP check, second file glob and second test command each name a nonce nothing in the workspace carries, so the reader that decides pass from fail is exercised in both directions.

- The checked-in fixture job directory is a live trial of the case as it stands, so its records carry the per-turn timings the timing facts read.

- `configs/diagnostics/fixture_harness_config.json` carries the nightly `haiku` cell's flags, one `--ak` kwarg per key, and `configs/diagnostics/fixture_expected_facts.json` is the expected-facts table `minds-evals check-diagnostics` grades a fixture job against. Its values are measured from a live trial, and a trimmed copy of that job directory is checked in so a wrong value fails in PR CI.

- The fact block gains `judge.unnamed_control_note` and `judge.addressed_by_ref_step_count`: whether the flow digest carries the note on controls the page gives no accessible name, and how many steps it marks as addressed by ref.

- `seed.in_head` reads the box's own `merge-base --is-ancestor` answer from `repo_state.json` rather than comparing the seeded SHA against the bundle's base, which on a seeded trial is the seeded commit itself.

- The registry rows a fact reads are stamped seeded as well as pre-existing, so a seeded row never reads as one the agent delivered.

- `git bundle verify` is given the bundle's absolute path, so `bundle.verified` no longer reads false whenever the job directory was named relative to the caller -- which is how the README and the scheduled job name it.

- `cleanup-environments --job-dir` finds a stepped trial's Modal environment. Harbor archives each step's `agent/` under `steps/<name>/` as the step ends, so a finished stepped trial has no state at its root and every environment such a case made was left behind.

- The fact block gains a `timing` family, on a case that declares a `timing` block: `timing.turn_index` names the client message whose reply satisfied the goal-holding client, `timing.seconds_within_elapsed` says the measured span is positive and sits inside the trial's `elapsed_seconds`, and `timing.agrees_with_feed` says the workspace's own event feed shows the exchange the span was measured over. The span is re-derived at check time from `state.json`'s `entries` and `turns` with the collector's own reader, so the first two facts say what the manifest's `time_to_goal` entry was measured from and the third is an independent record of it. The two records do not time the same thing -- the feed stamps the last agent message, the driver's span closes when the agent is next seen idle -- so the agreement asks that the span opens on the client message the feed stamps and closes at or after the reply it stamps, which is what a span anchored on the welcome turn fails.

- The fixture case declares a `timing` block whose `requires_no_failures` names the `http` class, so its `time_to_goal` entry records `failed` by construction and the prerequisite path is exercised beside the plain one. The table asserts the three timing facts and the entry's status; no fact reads the anchors, so the suite never judges whether a turn was fast or slow.

- `configs/eval-config-diagnostics-behaviour.json` is the self-diagnostic behaviour case: one two-step case whose agent runs exact `tk` step and ticket commands, a nonce echo, a missing command and a scripted worker launch in step `work`, then reads back an upload that arrives only with step `check` (`configs/datasets/diag-upload`) and the worker's report. Every literal carries the nonce `7f3a`, so no template text can match one by accident.

- `configs/diagnostics/behaviour_harness_configs.json` holds each harness's cheap diagnostic config as `--ak` kwargs: claude on `haiku` at effort `medium`, pi-coding on `openrouter/openai/gpt-5-mini` at effort `medium`, and codex on `gpt-5.6-luna` at effort `low`, all at standard speed. An entry may also name `unsupported_pairs`, the pairs whose workspace template offers its lane no pasted-key sign-in; the codex entry names `released`.

- `harness_for_lane` maps each provider lane to the harness it runs (`anthropic` to claude, `openai` to codex, `api-key`, `openrouter` and `opencode-go` to pi-coding), and `ci_matrix.select_nightly_harnesses` derives the behaviour cells' harnesses from the nightly entries of `configs/harness_configs.json`: claude, pi-coding and codex.

- A step config's `"diagnostic_probe": true` (a boolean; generation refuses anything else) makes that step's collection run a diagnostic probe -- the workspace's ticket files, its agent listing, the launch-task reports that exist and the uploaded files, kept as `verification/diagnostic_probe.txt` -- parsed by `diagnostic_probe.py`, which shares nothing with the collectors it is a second record for. A probe that did not answer writes the file with `failure_reason: <why>` on its first line, so an unrun probe never reads as a workspace that holds nothing.

- On such a step the driver also reads every tool call's full input from the chat app's per-event detail endpoint, in one exec, and records each payload in `agent/driver_events.jsonl` as an `event_detail` record carrying the `event_id` it belongs to. The polled feed carries a tool call's label and size only, so this is where what the agent actually ran is readable.

- The verifier keeps `judge_transcript.txt`, the judged transcript with its progress blocks, under `verifier/derived/` beside the progress summary, the failure counts and the flow digest.

- The README describes the behaviour cells and case, the diagnostic probe, and how to run one cell by hand.

- `behaviour_facts.py` computes the behaviour family's facts at check time, from the records the job directory holds, and folds them into the fact block of any step whose case config asks for the diagnostic probe. Every other step carries none, so a fixture trial is unaffected.

- The compliance facts -- `agent.step_tickets`, `agent.regular_ticket`, `agent.ran_nonce_echo`, `agent.ran_missing_command`, `agent.worker_launched`, `agent.worker_finished` and `agent.read_upload_marker` -- read only the diagnostic probe and the event feed's tool inputs, so a regression in the tickets capture, the agent listing, the captured trajectory or the verifier's renderers can never read as "the agent did not comply".

- The health facts say whether those sources answered at all: `probe.read` (the probe file is there, carries no `failure_reason` line and parses) and `feed.inputs_read` (every tool call the feed shows had its input read, which `feed.tool_call_count` and `feed.tool_calls_without_input` count), beside the existing `listing.complete`. A health miss is a failed verdict, never "not followed".

- The instrument facts are agreements between records written independently: `progress.step_ids_agree`, `progress.declared_titles_agree`, `progress.done_summaries_agree`, `progress.nonce_block_count`, `progress.rendered_block_count` and `progress.regular_ticket_rendered` across the tickets, the trajectory, the feed and the rendered progress timeline; `tools.nonce_in_one_executing_result`; `failures.missing_command_count` and `failures.missing_command_is_error`; `steps.upload_marker_present` and `steps.upload_marker_read`; and `workers.model_is_lead_model` and `workers.report_captured`.

- `configs/diagnostics/behaviour_expected_facts.json` is the family's expected-facts table: it grades the case `behaviour`, requires `prep.stage_reached@work` throughout, and spells each healthy value at its entry with the defect, and its mark, in a per-harness override. Its known failures are codex's `failures.missing_command_is_error`, `transcript.agent_steps_with_model_name`, `usage.tokens_present@check` and `arm.harness_config.is_model_confirmed`, and `workers.model_is_lead_model@check` on every harness -- `false` on claude and pi-coding (#1008) and `null` on codex (#898). All are strict, so a cell that stops recording one is red.

- `check-diagnostics` reads three more records per step: the diagnostic probe, and the verifier's `judge_transcript.txt`, `progress_summary.json` and `harness_failures.json`. A worker capture's `is_report_captured` is read back too.

- The behaviour table is graded in CI against each harness's own live cell, whose job directory is checked in under `test_fixtures/diagnostics_behaviour_jobs/` trimmed to the records the facts read, so a wrong table entry fails on the branch rather than on the night.

- The README documents the behaviour facts, which record each is read from, and how a cell is graded.

- Item 12 of the behaviour case's `work` step reaches for the `build-app` skill: claude invokes it through its own `Skill` tool, and the harnesses that have no such tool read the skill's file, which is what their reader looks for. `work` declares a `process` block beside it -- `build-app` and a nonce skill required, a nonce skill forbidden, one worker launch allowed -- so the collector's process checks run on a step whose only expectation is that block, and the four process entries record `passed`, `failed`, `passed` and `passed` by construction.

- `agent.invoked_skill` is a compliance fact, read from the event feed alone: a `Skill` call naming the skill, or a call whose input opens the skill's file. `process.invoked_skills` and `process.worker_launch_count` run the readers the collector's process checks run over the captured document, and `evidence.statuses@work` pins the four process entry ids and their statuses, so what the collector made of the captured stream and what those readers make of the captured document are a second record of each other.

- `evidence.statuses@work` names `file_inventory` beside the four process entries: the file inventory is part of the always-on capture, so its entry is in every step's manifest whatever the case declares.

- `steps.spend_deltas_sum@check` is a known failure on codex (#898): harbor records no per-step input tokens for a codex step, so the per-step deltas cannot be summed against the trial's running total at all.

The scheduled run checks the instrument every night: each resolved pair runs the fixture self-diagnostic and one behaviour self-diagnostic per nightly harness it supports, every live cell is read against the invariants that hold whatever the case, and the Slack report carries all of it.

- `minds-evals ci-matrix` emits `diagnose_fixture_matrix` (one entry per resolved pair), `diagnose_behaviour_matrix` (one entry per resolved pair and supported harness of the nightly set), `diagnose_unsupported` and `is_any_pair_diagnosed`, whatever the green markers say. Each entry carries the pair's refs and SHAs, the lane key variable, and the `--ak` flags built from `configs/diagnostics/fixture_harness_config.json` or `configs/diagnostics/behaviour_harness_configs.json` through the driver's own kwarg parsing. `--fixture-harness-config` and `--behaviour-harness-configs` name other files; both default to the checked-in ones, and a nightly harness with no behaviour config is refused.

- A behaviour entry's `unsupported_pairs` names the pairs whose workspace template offers its lane no pasted-key sign-in. That cell is left out of the matrix rather than run against a template that cannot sign it in, and is listed under `diagnose_unsupported` for the report: no box is spent on a dark cell, and no expected table carries a per-pair exception.

- A harness config named `diagnose` or starting with `diagnose-` is refused: its cell's summary artifact could take a diagnose job's name.

- `minds-evals ci-report` reads every diagnose summary of a pair, a pair whose every cell was skipped included. The pair's opening line names the worst diagnostic verdict and the jobs at it (`diagnostics failed: behaviour codex (3 facts)`), with any unsupported cell beside it. A `failed` diagnostic fails the pair and posts under `:brainless:`; `passed`, `known`, `not followed` and `not measured`, and a diagnose job that left no summary, leave the pair's verdict as its cells made it. Each failing fact is a row of the failed-trials table (config `diagnose fixture` or `diagnose behaviour <harness>`), and the details block names a job that left no summary, every not-measured and not-followed reason, the cells the pair cannot run, the live invariants each cell's own trials missed, and every known failure. The failures table now keeps within Slack's row cap and table character budget, and says how many rows it dropped. `--diagnose-fixture-result` and `--diagnose-behaviour-result` take the two jobs' results.

- `minds-evals check-diagnostics` writes its computed blocks as `<summary stem>-facts.json` rather than under one fixed name, so the summaries of several jobs merge into one directory without overwriting each other.

- `minds-evals cleanup-environments --job-dir` finds the environments of stepped trials, whose state is archived under `steps/<name>/agent/`.

- The diagnose jobs belong to the pair, not to one of the eval configs its suites name, so they ride on the pair's first Slack message: a pair whose night runs two suites reports one broken instrument once rather than once per suite. A cell's live-invariants summary carries the eval config's slug beside the pair and the arm, as every other summary name does, so two suites' cells cannot overwrite each other's reading.

- A *slug* named `diagnose` or starting with `diagnose-` is refused wherever a name becomes a job, artifact or cache-key component -- an eval config's as well as a harness config's -- since either half of a cell's name could otherwise take a diagnose job's artifact name.

- A trial that never reached its conversation reads as that one miss. The fixture family's expected-facts table requires `prep.stage_reached`, and the live-invariants table asserts a `prep.stage_reached` of `conversation` and requires it too, so a preparation that stopped short reports one failed fact and leaves the rest precondition-unmet instead of a failure per record the workspace never wrote. The count on the pair's opening line is of the facts that missed, never of the ones a table held back.

- A cell's `live invariants missed` detail line names each missed invariant once with the trials that missed it (`prep.stage_reached (3 of 3 trials)`), and a cell that missed none has no line.

- `steps.upload_marker_read` reads the upload's marker in any tool result of the check step, whatever tool produced it, so a harness that answers the item with its file-reading tool rather than a shell command reads the marker just as well.

