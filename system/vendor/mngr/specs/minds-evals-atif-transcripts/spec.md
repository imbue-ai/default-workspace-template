# minds_evals: grading from the ATIF trajectory

**Audience:** developers working on `apps/minds_evals` (the persona-eval driver, its evidence collector, and the verifier templates) and on the `mngr` common transcript.

**Status:** implemented.

This spec makes the ATIF trajectory document the only transcript a trial hands to grading.
The verifier reads `trajectory.json` and nothing else about the conversation, exactly as it would for any other harbor eval, and that document is the one mngr builds from the workspace agent's common transcript whenever the workspace can provide it.
It records what each consumer sees afterwards, and what happens when the workspace cannot provide the document.

Related:

- [`../atif-transcript-alignment/spec.md`](../atif-transcript-alignment/spec.md): the stream format, the doc-builder, and `mngr transcript --format atif`.
- `apps/minds_evals/README.md`: user-facing description of the trial artifacts and the evidence bundle.
- `apps/minds_evals/imbue/minds_evals/driver.py`, `evidence_collection.py`, `trajectory.py`, `generate.py`, and the verifier scripts under `templates/tests/`.

## Contents

- [Background](#background)
- [Goals and non-goals](#goals-and-non-goals)
- [Consumers of the transcript](#consumers-of-the-transcript)
- [Design](#design)
  - [One grading input: `trajectory.json`](#one-grading-input-trajectoryjson)
  - [Where the capture runs](#where-the-capture-runs)
  - [Why the workspace builds the document](#why-the-workspace-builds-the-document)
  - [The capture step](#the-capture-step)
  - [What `trajectory.json` contains](#what-trajectoryjson-contains)
  - [The verifier scripts](#the-verifier-scripts)
  - [Trial metadata](#trial-metadata)
- [Failure modes and fallbacks](#failure-modes-and-fallbacks)
- [What changes for each consumer](#what-changes-for-each-consumer)
- [Testing](#testing)
- [Deliberately left out](#deliberately-left-out)

## Background

Each trial writes its artifacts under `<job>/<trial>/agent/`.
Two transcript pipelines exist inside the nested workspace:

- **Pipeline A, the UI feed.**
  The workspace `system_interface` tails the raw claude session files and parses them with its own re-implementation of the pre-ATIF converter.
  It serves legacy `user_message` / `assistant_message` / `tool_result` records over `GET /api/agents/<chat_id>/events`, with tool inputs cut to previews, tool outputs truncated, and thinking dropped.
  The driver polls this feed (`fetch_events_window`) to detect the agent's reply after each turn.
- **Pipeline B, the common transcript.**
  mngr's own claude emitter writes `$MNGR_AGENT_STATE_DIR/events/claude/common_transcript/events.jsonl`: an ATIF-shaped stream (`header`, `step`, `observation` records) at full fidelity.
  `mngr transcript <agent> --format jsonl` prints the stream and `--format atif` assembles it into a validated ATIF document, embedding the trajectories of claude subagents that mngr ran as proxy siblings.

Before this change, grading ran on artifacts made from pipeline A: `full_transcript.jsonl` (the polled feed, verbatim) fed the judge-transcript renderer, and `conversation.jsonl` (the driver's clean per-turn record) fed the structural gates and the wordiness guard.
`trajectory.json` was a hand-built summary written for `harbor view` only: one ATIF step per clean conversation turn, `final_metrics` from the resolved workspace usage, `agent.model_name` set to the decider model.

Other harbor evals have no rendered feed at all; their only conversation record is `trajectory.json`.
A verifier that reads anything else cannot be shared with them.

## Goals and non-goals

**Goals**

- All grade-time work reads `trajectory.json` (plus `state.json` and the evidence bundle) and is unaware that a rendered UI feed exists, so the verifier is the same shape as any other harbor eval's.
- `trajectory.json` is the real ATIF document the workspace builds, not a hand-built summary, with the fields the eval needs (resolved usage, decider provenance) reconciled onto it.
- The document is in the box after every turn, so a trial that dies mid-way still leaves a gradeable record and a regradable set of artifacts.
- Any capture failure degrades to the hand-built document, best-effort like every other piece of evidence.
- No change to `libs/mngr`.

**Non-goals**

- Backwards compatibility with pre-ATIF (`user_message` / `assistant_message`) records anywhere at grade time.
  The verifier reads ATIF steps only.
- Re-pointing the driver's turn detection or its usage accounting at the common transcript.
  Both read the feed the driver polls, which is the product's real-time view; the feed's record vintage is a driver-side concern and is invisible to grading.
- Redaction of the captured transcript.
  The raw session files sit unredacted in the same snapshots.

## Consumers of the transcript

| Consumer | Reads | What it needs |
|---|---|---|
| `driver._new_agent_reply_texts` (reply detection) | the in-memory events the driver polled | agent reply texts after the send-time baseline |
| `usage.summarize_workspace_usage` | the in-memory events the driver polled | per-message model and tokens |
| word-count metadata (`average_words_per_turn`, `average_words_per_message`) | the reply texts the driver polled | per-message and per-turn word counts |
| `templates/tests/render_judge_transcript.py` (verifier) | `/logs/agent/trajectory.json` | one `[USER]` block per `user` step, one `[AGENT · message N]` block per `agent` step with a non-empty `message` |
| `templates/tests/gates/checks.py` (verifier) | `trajectory.json`, `state.json` | at least one non-empty agent message after the first `user` step; distinct, non-stub agent messages; the turn and timeout state |
| `templates/tests/quality/wordiness.py` (verifier) | `trajectory.json` | words per agent turn, one turn being the agent messages between consecutive `user` steps (nothing before the first) |
| the quality and outcome judges (verifier) | `judge_transcript.txt` | the rendered conversation |
| `harbor view` | `agent/trajectory.json` | a valid ATIF document |
| harbor's `agent_result` fields | `context.n_input_tokens` etc. | the resolved workspace usage (proxy when metered) |

The reply detection, the usage summary, and the word-count metadata read what the driver polled *during* the conversation.
They are the driver's own instruments, not grading inputs, and this spec leaves them alone.

## Design

### One grading input: `trajectory.json`

The task declares three artifacts: `/logs/agent/trajectory.json`, `/logs/agent/state.json`, and `/logs/agent/verification`.
`full_transcript.jsonl` and `conversation.jsonl` are no longer written.
Harbor collects declared artifacts from the box and re-uploads them into the verifier container, so the document has to be **in the box**, not only in the host logs directory.

The driver keeps `trajectory.json` current in the box the way it kept the jsonl files current:

- After every turn (and on every state change), `_sync_trial_files` writes the **hand-built** document from the clean conversation so far, beside `state.json`, into the host logs dir and the box.
  This is the partial record a timed-out or crashed trial leaves behind.
- After the evidence phase, `_publish_trajectory` writes the **final** document, the workspace's own when it was captured, else the hand-built one, to the host and uploads it into the box.
  Nothing writes the file again after that.
  If that upload fails, the last per-turn copy is what the box (and therefore the verifier, and the host after harbor's log download) holds, so the driver restores the host copy to the hand-built shape and reports `hand_built`.

A trial that never exchanged a message has no hand-built steps, and ATIF requires at least one, so unless the evidence phase captured the workspace's own document (whose greeting steps make it valid ATIF) it gets no `trajectory.json` and `trajectory_source` is `none`.
Its structural gates fail on the absent file and on `state.json`, which is the right grade; harbor records the missing declared path, which such a trial gives no reason to regrade.

### Where the capture runs

The common transcript lives inside the nested workspace, which is destroyed by the driver's teardown.
The only window in which it can be read is the evidence-collection phase, which already runs against the live workspace before teardown, for every trial that got as far as a workspace (timed-out trials included).
The capture is therefore an always-on step of `EvidenceCollector`, run right after the workspace-state probe and before the file inventory, so the cheap, high-value capture never waits behind the home-tree walk.

The driver's per-turn polling stays on the UI feed:

- Turn detection is the driver's core loop, and the UI feed is the product's own real-time view with `offset`/`limit` windowing and a cheap `total` head request.
  The common transcript is flushed by a five-second daemon plus turn-end hooks, so a poll on it would add emitter lag to every turn and need byte-offset windowing of its own.
- The usage account and the word counts are derived from the same polled events, so they stay consistent with reply detection.

### Why the workspace builds the document

Three routes were available for producing the ATIF document:

1. Run `mngr transcript <chat_agent_id> --format atif` inside the live workspace and pull the file out.
2. Pull a copy of `events.jsonl` out and build the document host-side with `parse_stream_content` and `build_trajectory_from_records` from `libs/mngr`.
   This is feasible: `apps/minds_evals` depends on `imbue-mngr-usage`, an editable path source whose transitive `imbue-mngr` resolves to this checkout, so `imbue.mngr.agents.trajectory_build` imports from the standalone venv.
3. Add a file-input mode to `mngr transcript` in `libs/mngr` and run that host-side on the pulled copy.

Route 1 is taken.
The document `mngr transcript --format atif` builds embeds the trajectories of claude subagents that mngr ran as proxy siblings, and resolving those requires the workspace's own agent discovery (the sibling agents' labels) which routes 2 and 3 cannot see from outside.
Route 2 would also duplicate the CLI's root enrichment (agent type, session and trajectory ids) in this app.
Route 3 changes `libs/mngr` for no gain over route 1.

The trade-off route 1 accepts is that the capture depends on `mngr` being on the exec path inside the workspace and being new enough to know `--format atif`.
Both are true for any workspace built from a post-ATIF mngr SHA, because the driver overwrites the workspace template's vendored mngr with the mngr under test.
When either is false the document half fails visibly and the trial grades on the hand-built document (see [Failure modes and fallbacks](#failure-modes-and-fallbacks)).

### The capture step

One bridged exec runs inside the workspace (`transcript_capture_command` in `evidence_collection.py`):

```sh
mkdir -p /tmp/minds-evals-verification;
mngr transcript <chat_agent_id> --headless --format jsonl > <staging>/common_transcript.jsonl 2> <staging>/transcript.err;
printf '<<<MINDS_EVALS_SECTION:stream_exit>>>\n%s\n' "$?";
mngr transcript <chat_agent_id> --headless --format atif --output <staging>/workspace_trajectory.json 2>> <staging>/transcript.err;
printf '<<<MINDS_EVALS_SECTION:document_exit>>>\n%s\n' "$?";
printf '<<<MINDS_EVALS_SECTION:stderr>>>\n'; tail -c 4000 <staging>/transcript.err;
exit 0
```

The command prints only the two exit codes and a bounded stderr tail, so the `mngr exec --format json` envelope never carries the transcript itself; the files travel by the same `mngr rsync` pull the inventory and the git bundle use (`_pull_staged_path`), into the box's `/logs/agent/verification/`.
The collector then copies each pulled file to the host-side bundle directory with the harbor environment's `download_file`, which is a filesystem transfer rather than an exec, so a large document is never streamed through a command's stdout.

The two halves are recorded independently as a `TranscriptCapture` on the collector: for the stream and for the document, the host path when captured, else a reason (`bridge_failed`, `timeout`, `transcript_command_failed`, `pull_failed`, `download_failed`) and a bounded detail (the stderr tail).
The command rides the collector's trace like every other probe, and the phase is timed as `common_transcript`.

The stream half is evidence only.
The document is what grading reads; the verbatim stream copy stays in the bundle as `verification/common_transcript.jsonl` so the document can be checked against what the workspace actually wrote, and `--format jsonl` is used rather than `cat` on the raw file because it resolves the agent type and the host directory itself.

The capture writes **no manifest entries**.
The manifest indexes outcome checks the verifier scores, it is shown to the outcome judge, and `is_evidence_complete: false` is presented there as "a check could not be measured".
A capture failure would otherwise flag a trial's deliverable evidence as incomplete over a fact about the transcript.
The capture's outcome is reported through the trial metadata instead (see [Trial metadata](#trial-metadata)).

### What `trajectory.json` contains

When the document was captured, `trajectory.json` is the workspace's ATIF document with three reconciliations, made in `trajectory.py` and validated through harbor's own `Trajectory` model before writing:

- **`final_metrics`** is replaced with the resolved workspace usage (the proxy's account when a proxy metered the trial, else the transcript's), the same figures harbor's `agent_result` carries, so the two never disagree.
  `total_steps` stays the number of steps in the document.
  When the resolved usage saw no messages at all, the document's own per-step sums are kept.
- **`extra.minds_evals`** records what the document cannot know: the driver's name and version, the decider model, one entry per decider-model call (`turn`, the 1-based message it sent or null when it sent none; `entry_index`, `exchange`, and `entry_kind` locating the call; `model`, `is_fallback`, and the `detail` a call that ended its entry gave), the harbor session id, the case id, the usage source, and `source: "workspace"`.
- Everything else (`session_id`, `trajectory_id`, `agent`, `steps`, `subagent_trajectories`, `schema_version`) is the workspace's, untouched.
  `agent.name` is the workspace agent type (`claude`), which is what the trajectory describes; the decider is described only under `extra.minds_evals`.

Decider turns are recorded **beside** the steps, not appended as steps.
The decider's messages already appear in the document as the `user` steps the workspace recorded, so appending them would duplicate them, and mutating steps would break the document's provenance (`step.extra.event_id`) and its step-id sequence.

The hand-built document, written per turn and as the fallback, has one `user` or `agent` step per clean conversation turn (an agent turn being the driver's merge of that turn's reply messages), the driver as `agent`, the same `extra.minds_evals` block with `source: "hand_built"`, and `final_metrics` from the resolved usage.

The workspace's unmodified document stays in the bundle as `verification/workspace_trajectory.json`.

### The verifier scripts

Every grade-time reader takes the conversation from `trajectory.json`'s `steps` and understands ATIF steps only:

- `render_judge_transcript.py` writes `judge_transcript.txt`: one `[USER]` block per `user` step and one `[AGENT · message N]` block per `agent` step with a non-empty `message`, N running across the whole conversation.
  `system` steps (framework-injected text, compaction summaries) and steps with no message (tool-only inferences) render nothing.
  Both the quality judge and the outcome judge read this rendering; the outcome judge used to read `conversation.jsonl`.
- `gates/checks.py` takes the agent replies from the messages of the `agent` steps that follow the first `user` step.
  The workspace's own document opens with the agent's welcome greeting (after the `/welcome` skill body, a `system` step); that greeting answers no client turn and is not a reply, so it can neither satisfy `transcript_has_agent_reply` on its own nor pair with a single stub to pass `agent_engaged_substantively`.
  `transcript_has_agent_reply` and `agent_engaged_substantively` keep their meaning; the turn and timeout gates still read `state.json`.
- `quality/wordiness.py` takes words per agent turn as the merged messages of the `agent` steps between consecutive `user` steps, which on the hand-built document is one step per turn and on the workspace document is every inference the agent made for that turn.
  Agent messages before the first `user` step (the welcome greeting) belong to no turn and are not counted.

On the workspace document the judge sees one block per inference, which is exactly the per-message granularity the conciseness criterion wants; on the hand-built fallback it sees the per-turn merge, as it did before.

### Trial metadata

`context.metadata` carries:

- `trajectory_source`: `"workspace"` or `"hand_built"`, which shape `trajectory.json` has, or `"none"` when no trajectory was written because there was no conversation.
- `transcript_capture`: the per-half outcome, `{"stream": {"is_captured": bool, "reason": str, "detail": str}, "document": {...}}`.

`state.json` is unchanged.

## Failure modes and fallbacks

| Situation | Stream | Document | `trajectory.json` |
|---|---|---|---|
| Workspace built from a pre-ATIF mngr (`--format jsonl` answers with legacy-shaped records, `--format atif` is unknown) | captured (legacy shape, evidence only) | `transcript_command_failed` | hand-built; stderr tail in metadata |
| No `mngr` on the workspace's exec path | `transcript_command_failed` | `transcript_command_failed` | hand-built; stderr tail in metadata |
| The document fails to build or validate (`mngr transcript` reports a build error, or harbor's model refuses it host-side) | captured | `transcript_command_failed`, or captured but refused | hand-built |
| The bridged exec fails or the phase budget is gone | `bridge_failed` / `timeout` | same | hand-built |
| `mngr rsync` pull fails | `pull_failed` | independent | hand-built when the document half failed |
| `download_file` into the host bundle fails | `download_failed` | independent | hand-built when the document half failed |
| No workspace was ever created, or its chat agent was never resolved | `not_attempted` | `not_attempted` | not written: no exchange happened, so `trajectory_source` is `none` |
| The collector raises before the capture step | `not_attempted` | `not_attempted` | hand-built |
| The collector raises mid-phase (any later step) | whatever was recorded before the raise | same | the driver reads the capture off the collector like it reads the verifier usage |
| The final upload of `trajectory.json` into the box fails | n/a | captured | the last per-turn hand-built copy stands in the box; the host copy is restored to match and `trajectory_source` reports `hand_built` |
| No exchange ever happened | independent | independent | the workspace's own document when it was captured and validates; otherwise not written, and `trajectory_source` is `none` |

Every failure is recorded, never raised: the evidence phase must not discard a completed trial or block teardown.

The capture reads the stream as it stands at collection time.
The emitter flushes on turn end and every five seconds, and the driver's last event refresh precedes collection, so a finished trial's document is complete; a trial timed out mid-turn may lack the last few seconds of the agent's work, which is the same window the polled feed misses.

## What changes for each consumer

- **Reply detection, usage accounting, word-count metadata:** unchanged; they read the polled feed.
- **`render_judge_transcript.py`:** reads `trajectory.json` steps; the legacy record handling and the `/welcome` special case are gone (the claude emitter never records slash-command plumbing, and the hand-built document never had it).
- **Structural gates and the wordiness guard:** read `trajectory.json` steps instead of `conversation.jsonl`.
- **Outcome judge:** reads `judge_transcript.txt` instead of `conversation.jsonl`; its prompt describes the rendering.
- **`decider_message` audit records:** gone with `full_transcript.jsonl`; the decider's provenance lives in `extra.minds_evals.decider_turns`, and its message text is the corresponding `user` step.
- **`trajectory.json` / `harbor view`:** the real document with tool calls, observations, thinking, and embedded subagents, instead of a turn summary; present in the box after every turn.
- **Evidence bundle:** `common_transcript.jsonl` and `workspace_trajectory.json`, present when captured; no new manifest entries.
- **Oracle runs (`-a oracle`):** the generator writes a hand-built ATIF trajectory of the canned conversation into `solve.sh` instead of the two jsonl files, so the oracle exercises the same verifier path a real trial does.
- **Task declaration:** `artifacts` lists `trajectory.json`, `state.json`, and `verification`.

## Testing

Unit tests in the existing `_test.py` files, driven through the scripted `MockBoxEnvironment` (with a scripted `download_file` so a pulled file can be served back to the host, and a scripted upload rejection):

- `evidence_collection_test.py`: the capture command's shape; a healthy workspace captures both halves into the bundle and the collector reports the host paths; a workspace with no `mngr` records `transcript_command_failed` for both halves with the stderr tail as detail and writes no manifest entry; a pre-ATIF workspace keeps the stream and records only the document as `transcript_command_failed`; a failed pull and a failed download are recorded per half; the phase is timed and traced.
- `trajectory_test.py`: the workspace document gets the resolved usage as `final_metrics` and the `extra.minds_evals` block while its steps, ids, agent, and embedded subagents survive; the document's own sums are kept when the usage is empty; the hand-built fallback carries the same block; an invalid document is rejected rather than written.
- `driver_test.py`: end to end against the mock box, `trajectory.json` is in the box after every turn as the hand-built document; a captured document replaces it (host and box) with the reconciled workspace document and the metadata says `workspace`; a failed capture, an unusable captured document, and a failed final upload each leave the hand-built document and say `hand_built`; no `conversation.jsonl` or `full_transcript.jsonl` is written.
- `render_judge_transcript_test.py` and `verifier_criteria_test.py`: the renderer, the gates' reply extraction, and the wordiness per-turn counts, each loaded by file path from `templates/tests/`, over the workspace-shaped and the hand-built-shaped document.
- `generate_test.py`: the task declares the three artifacts, the outcome judge lists `judge_transcript.txt`, and the oracle's `solve.sh` writes a `trajectory.json` with one step per prompt and reply.

No libs/mngr change, so `just test-minds-evals` covers everything.

## Deliberately left out

- **Re-pointing the driver's poll or its usage account at the common transcript.** See [Where the capture runs](#where-the-capture-runs).
- **A file-input mode for `mngr transcript`.** See [Why the workspace builds the document](#why-the-workspace-builds-the-document).
- **Pulling the subagents' own streams.** They are embedded in the document `mngr` builds (for proxy-run subagents); nothing further is captured for them.
- **Pricing the document's per-step metrics.** `final_metrics.total_cost_usd` is the resolved trial cost; per-step `cost_usd` stays absent as the stream leaves it.
- **A raw copy of the polled UI feed in the artifacts.** It was only ever a debugging aid; the common-transcript stream in the bundle is the higher-fidelity record of the same session.
