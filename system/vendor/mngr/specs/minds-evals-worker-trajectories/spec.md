# minds_evals: capturing background workers' trajectories

**Audience:** developers working on `apps/minds_evals` (the driver, the evidence collector, `trajectory.py`, `usage.py`) and on the `default-workspace-template` launch-task skill.

**Status:** implemented.

A workspace agent can hand work to a **background worker**: a separate mngr agent inside the same workspace, created by the launch-task skill's `create_worker.py`.
That worker's conversation, tool use, and tokens never appear in the chat agent's common transcript, so the trial's `trajectory.json` shows the launch and nothing after it, and the trial's cost understates what was spent.
This spec adds a capture of every worker the chat agent launched, and embeds each worker's ATIF trajectory in `trajectory.json` under the tool call that launched it.

It assumes the workers have **completed and settled** by the time evidence is collected: the lead has read their reports and they are either stopped in place or destroyed.
Workers still running at collection time are out of scope here.

Related:

- [`../minds-evals-atif-transcripts/spec.md`](../minds-evals-atif-transcripts/spec.md): how the chat agent's trajectory is captured and reconciled; this spec extends that capture step and that reconciliation.
- [`../atif-transcript-alignment/spec.md`](../atif-transcript-alignment/spec.md), "Subagent embedding": the `subagent_trajectories` / `subagent_trajectory_ref` convention and the `extra.subagent_kind` vocabulary.
- `default-workspace-template/.agents/skills/launch-task/scripts/create_worker.py`: how workers are created, awaited, and destroyed.

## Contents

- [Background](#background)
- [Goals and non-goals](#goals-and-non-goals)
- [What a settled worker looks like](#what-a-settled-worker-looks-like)
- [Design](#design)
  - [Discovery: which workers to capture](#discovery-which-workers-to-capture)
  - [Capture: one exec per worker](#capture-one-exec-per-worker)
  - [Transfer: the workers directory](#transfer-the-workers-directory)
  - [Reconciliation: embedding under the launching call](#reconciliation-embedding-under-the-launching-call)
  - [Usage: pricing the delegated work](#usage-pricing-the-delegated-work)
  - [Bundle layout and metadata](#bundle-layout-and-metadata)
  - [What grading sees](#what-grading-sees)
- [Failure modes](#failure-modes)
- [Alternatives considered](#alternatives-considered)
- [Testing](#testing)
- [Effort](#effort)

## Background

In a real trial (`jobs/todo-atif-inspect/todo-app__MXMV7ES`), step 43 of the chat agent's trajectory is a Bash call:

```
uv run .agents/skills/launch-task/scripts/create_worker.py launch --name crystallize-todo --template worker \
    --runtime-dir data/.tasks/harden/crystallize-todo/ --task-file data/.tasks/harden/crystallize-todo/task.md
```

Its observation reports `Creating agent state... Starting agent crystallize-todo ... Done.` and `Message sent to: crystallize-todo`.
Step 44 backgrounds `create_worker.py await --name crystallize-todo ...`.
The worker then runs as its own mngr agent, with its own common transcript, and the chat agent's document never mentions it again.

mngr's doc-builder (`libs/mngr/imbue/mngr/api/trajectory.py`) embeds only **proxy subagents**: sibling agents carrying the `mngr_claude_subagent_proxy_parent_id` and `mngr_claude_subagent_proxy_tool_use_id` labels, which the proxy plugin stamps because it knows the delegating tool call's id.
A worker carries neither.
`create_worker.py launch` runs `mngr create <name> -t worker --label agent_created=true`; the `lead_agent` it logs is written into the task file's YAML frontmatter, not onto the agent.
A Bash command cannot know its own `tool_use_id`, so no label can carry it either.

So the launch is visible in the chat agent's stream, the worker is a normal agent in the same workspace, and the only thing that ties the two together is the `--name` in the launching command.

## Goals and non-goals

**Goals**

- Every worker the chat agent launched (and any worker a worker launched) is captured during evidence collection, as the ATIF document `mngr transcript --format atif` builds for it, the raw stream, and the report it pushed back to its lead.
- The trial's `trajectory.json` embeds each worker under the tool call that launched it, in the ATIF v1.7 `subagent_trajectories` form, so `harbor view` and any ATIF consumer see the delegated work.
- The trial's transcript usage account includes the workers' tokens, and `is_cost_complete` is true when every launched worker was captured.
- Workers that were destroyed after finishing are captured from mngr's preserved copy of their stream.
- No change to `libs/mngr` and no change to the workspace template.

**Non-goals**

- Workers still running at collection time: their partial trajectories are captured as whatever the stream holds, but nothing waits for them, and no completeness claim is made about them.
  Their spend so far is folded into the transcript account, but they do not count toward `worker_captured_count`, so `is_cost_complete` stays false for the trial.
- Proxy subagents (the `Agent` tool): mngr already embeds them; pricing their embedded steps is a separate follow-up.
- Showing worker conversations to the judges: a worker talks to no client, and the grade-time readers keep rendering only the root document's steps.

## What a settled worker looks like

From `create_worker.py` and the launch-task skill:

- `launch` creates the agent (`mngr create <name> -t worker --label agent_created=true`, type `claude`), rsyncs the runtime directory into its worktree, and sends it the task file.
- The worker works, writes its report to the `finish_report_path` named in the task frontmatter, and pushes it back to the lead's runtime directory; the lead's backgrounded `await` returns when the report lands.
- After that the worker sits **stopped in place** (`WAITING`/`STOPPED`).
  `launch-sync` destroys it by default once the report is in (`--keep-agent` skips that); the async `launch`/`await` flow leaves it unless the lead runs `create_worker.py destroy`.
- Destroying preserves the agent's sessions: the stream moves from `$MNGR_HOST_DIR/agents/<id>/events/claude/common_transcript/events.jsonl` to `$MNGR_HOST_DIR/preserved/<name>--<id>/events/claude/common_transcript/events.jsonl` (`preserve_sessions_on_destroy` defaults to true).
  `mngr transcript` no longer resolves a destroyed agent; its stream has to be read from that path.

| Worker state at collection | Listed by `mngr list` | `mngr transcript <name> --format atif` | Stream |
|---|---|---|---|
| stopped in place | yes, with `id`, `state`, `labels`, `create_time` | works | `agents/<id>/events/claude/common_transcript/events.jsonl` |
| destroyed | no | fails (unknown agent) | `preserved/<name>--<id>/events/claude/common_transcript/events.jsonl` |

The workspace's host dir is `/home/user/.mngr`; every agent's exec environment carries it as `MNGR_HOST_DIR`.

## Design

### Discovery: which workers to capture

Workers are discovered from the chat agent's own stream, not from the agent listing.
After the existing capture step has pulled `verification/common_transcript.jsonl` to the host, a pure function scans its `agent` steps' tool calls for launch commands:

```
create_worker\.py\s+(?:launch|launch-sync)\b(?:\\\n|[^\n])*?--name(?:=|\s+)(?P<name>\S+)
\bmngr\s+create\s+(?P<name>[^\s-]\S*)
```

The arguments run across backslash-continued lines, which is how the skill's own snippet spells the call.
Each option's value is read after whitespace or an `=`, as argparse takes it, with surrounding quotes dropped; a `$NAME` anywhere in a value (the name itself, or the task-file path the skill writes as `data/.tasks/launch-task/$NAME/task.md`) is replaced by the `NAME=...` assignment earlier in the same command when there is one (an assignment's own value expanded against the assignments before it, as the shell does), and otherwise kept as written, so the launch is still counted and then recorded as one that could not be captured.
`--task-file` is read the same way.

Each match yields a `WorkerLaunch(name, tool_call_id, task_file, depth, lead_name)`: `task_file` is the `--task-file` argument when the command carries one, `depth` is 0 for the chat agent's launches and one more per nesting level, and `lead_name` is the worker that launched it (empty for the chat agent).
`await`, `destroy`, and `mngr message` calls are ignored: they refer to a worker already found.
Discovery keys on the launch text because it is the only join between the launching step and the worker, it names destroyed workers the listing no longer shows, and it excludes the other `agent_created` agents in a workspace (automations, the caretaker) that the label alone would sweep in.

Discovery is **iterative**: once a worker's stream is captured, the same scan runs over it, so a worker's own workers are captured too.
Rounds are bounded (`MAX_WORKER_ROUNDS = 3`) and so is the total (`MAX_WORKER_COUNT = 100`, a ceiling no real trial should reach, there to keep a runaway launch loop from consuming the phase budget); a trial that exceeds either records the overflow and stops.
Names are de-duplicated across rounds and across leads; a name launched twice is captured once, as whatever agent currently answers to it, under its first launch, and a later launch of the same name is not recorded separately.

### Capture: one exec per worker

The listing is captured first, in one bridged exec, so every worker has an id, type, state, and work dir (a launch's `--task-file` is relative to its lead's work dir).
The listing rides the exec output as a section of its own, because the collector needs it before any transfer has happened:

```sh
mkdir -p <staging>/workers; mngr list --headless --format json > <staging>/workers/agents.json 2> <staging>/workers/list.err;
printf '<<<MINDS_EVALS_SECTION:list_exit>>>\n%s\n' "$?";
printf '<<<MINDS_EVALS_SECTION:listing>>>\n'; cat <staging>/workers/agents.json;
printf '<<<MINDS_EVALS_SECTION:stderr>>>\n'; tail -c 4000 <staging>/workers/list.err; exit 0
```

A listing that cannot be had is logged and the workers are captured by name alone: their ids, states, and lead work dirs then come from the preserved directories or stay empty.

Then one bridged exec per discovered worker, in launch order (`worker_capture_command(name, lead_work_dir, task_file)`):

```sh
d=<staging>/workers/<name>; mkdir -p "$d";
mngr transcript <name> --headless --format atif --output "$d/trajectory.json" 2> "$d/transcript.err";
printf '<<<MINDS_EVALS_SECTION:document_exit>>>\n%s\n' "$?";
mngr transcript <name> --headless --format jsonl > "$d/common_transcript.jsonl" 2>> "$d/transcript.err";
printf '<<<MINDS_EVALS_SECTION:stream_exit>>>\n%s\n' "$?";
printf '<<<MINDS_EVALS_SECTION:preserved>>>\n';
if [ ! -s "$d/common_transcript.jsonl" ]; then
  p=$(ls -td "${MNGR_HOST_DIR:-/home/user/.mngr}/preserved/<name>--"*/ 2>/dev/null | head -n 1);
  if [ -n "$p" ]; then cp "$p"events/*/common_transcript/events.jsonl "$d/common_transcript.jsonl" && printf '%s\n' "$p"; fi;
fi;
printf '<<<MINDS_EVALS_SECTION:report_path>>>\n';
r=$(sed -n 's/^finish_report_path:[[:space:]]*//p' <lead work dir>/<task_file> | head -n 1); printf '%s\n' "$r";
printf '<<<MINDS_EVALS_SECTION:report_exit>>>\n';
if [ -n "$r" ]; then mkdir -p "$d/reports" && cp -R "<lead work dir>/$(dirname "$r")/." "$d/reports/"; printf '%s\n' "$?"; fi;
printf '<<<MINDS_EVALS_SECTION:stderr>>>\n'; tail -c 4000 "$d/transcript.err"; exit 0
```

The report is captured from the **lead's** side, not the worker's: the launch-task contract has the worker write its report to the `finish_report_path` named in the task file's frontmatter and push it back into the lead's runtime directory, where the lead reads it and moves it to `consumed/` once handled.
The task file path comes from the launch command's `--task-file`, relative to the lead's work dir (`/home/user/workspace` for the chat agent; a nested worker's own worktree for its launches); an absolute one is taken as written.
Copying the whole reports directory (`report.md` plus `consumed/`) captures the report whether or not the lead has consumed it yet.
A launch that names no task file, or a task file that names no `finish_report_path`, records no report and says so; a named path whose directory is not there records the copy's failure.

The report also reaches the lead's own trajectory, but only indirectly: when the backgrounded `await` returns, Claude Code injects a task notification carrying the await's printed output, which the emitter records as a step.
That is a quotation inside a notification, present only once the await has returned and only as far as the notification quotes it, which is why the file is captured explicitly.

One exec per worker rather than one for all: the section-marker vocabulary is per exec, workers are few (zero to two in practice), each exec costs a few seconds, and the per-worker outcome then records itself the way the chat agent's capture does.
Every exec is clamped to the phase budget like every other probe; once the budget is gone each remaining worker's capture exec is skipped and its launch is recorded with every part `timeout`, and a round that staged nothing is not transferred.

The preserved fallback picks the **newest** `<name>--<id>` directory, because a name can be reused after a destroy; the chosen directory's basename is what tells the host side the destroyed worker's id.

### Transfer: the workers directory

After the last worker exec, the staging directory is pulled into the box in one rsync, and then downloaded to the host in one transfer:

```
mngr rsync <workspace>:<staging>/workers/ /logs/agent/verification/workers/
environment.download_dir("/logs/agent/verification/workers", <host bundle>/workers)
```

This needs two small additions to the collector: a directory mode of `_pull_staged_path` (trailing-slash rsync of a directory into a directory of the same name), and a `_download_evidence_dir` beside `_download_evidence` (harbor's `download_dir`).
Both follow the existing best-effort pattern: a failure is recorded on every worker of that round, never raised.

### Reconciliation: embedding under the launching call

Reconciliation is pure and lives in `trajectory.py`, run by the driver after the workspace document has been reconciled as today.

**Each worker's trajectory** is taken from its captured `trajectory.json` (the document `mngr transcript --format atif` built inside the workspace, which already carries the worker's own proxy subagents and its `session_id`/`trajectory_id` as the worker's agent id).
For a destroyed worker only the stream exists, so its document is built host-side with mngr's own pure builder, `parse_stream_content` and `build_trajectory_from_records` from `imbue.mngr.agents.trajectory_build` (importable from this app), enriched with the listing's agent type as `agent_name` (`"claude"`, the type the launch-task skill creates, when the listing no longer shows the worker), `agent_version = "unknown"`, and the id from the preserved directory's basename.
Either way the result is the same shape.

**Grafting** onto the root document, given the launches and the worker documents:

1. Find the root step whose `tool_calls` contains the launch's `tool_call_id`.
2. In that step's `observation.results`, find the result whose `source_call_id` is that id and append a `subagent_trajectory_ref` `{"trajectory_id": <worker id>, "extra": {"subagent_kind": "mngr", "worker_name": <name>}}`, keeping the textual result as the quick-reference summary.
   A settled worker's launch always has its result (it is the `Creating agent state...` output); if one is ever missing, a placeholder result is synthesized the way mngr's builder does (`extra.subagent_result_pending: true`).
3. Set `extra.subagent_kind = "mngr"` and `extra.worker = {"name", "agent_id", "state", "lead_agent_id", "launch_tool_call_id", "report_path"}` on the worker document and append it to the root's `subagent_trajectories`.
4. Repeat inside each worker document for the workers it launched, before it is embedded, so nesting is represented as ATIF nesting.
5. Validate the whole document through harbor's `Trajectory` model, which enforces unique `trajectory_id`s among embedded trajectories and that every ref resolves.

`subagent_kind` stays mngr's own `mngr` value: the ATIF alignment spec defines `mngr` as "an agent mngr ran as a sibling in the same workspace" against `native` for a sidechain carved out of one transcript, and a worker is exactly such a sibling; the only difference from a proxied `Agent` call is how it was delegated, which the launching step itself shows.
What makes a worker recognisable is the `extra.worker` block on the embedded trajectory and `extra.worker_name` on the ref, so no new vocabulary is introduced.

`final_metrics` stays the trial's resolved usage, which after this spec includes the workers (below).
ATIF's own reading of `total_cost_usd` is "including cost for subagents", so the two agree.

When the root document is the **hand-built** fallback (the workspace document was not captured), there is no launching step to graft onto; the workers stay in the bundle and in the metadata, and `trajectory.json` carries no `subagent_trajectories`.

### Usage: pricing the delegated work

`usage.summarize_workspace_usage` already reads ATIF `step` records with their `metrics`, which is exactly what a worker stream holds, so a worker's `TrialUsage` is `summarize_workspace_usage(worker_stream_records)` unchanged.

The **transcript account** becomes the sum of the chat agent's account and every captured worker's account, added bucket by bucket per model (a `combine_trial_usages` helper; `TrialUsage` already has the per-model breakdown this needs).
`is_cost_complete` is redefined as: no `Agent`-tool delegations *and* every launched worker was captured with a stream.
`worker_launch_count` is taken from the launch scan of the captured stream, and a new `worker_captured_count` reports how many of them the capture brought out settled (a worker whose listing state is still running has a stream that is spend so far, not its whole spend, so it is summed but not counted; one whose state could not be established, because the listing failed or named a state mngr's enum does not know, is treated the same way).

The **proxy account** is untouched: it already includes worker traffic, because every agent in the workspace shares the trial's credential, so nothing is added on top of it.
When a proxy metered the trial, the worker accounts are still computed and reported per worker, which is what lets the two accounts be reconciled after the fact.

### Bundle layout and metadata

```
verification/
  workers/
    agents.json                       # `mngr list --format json` at collection time
    list.err                          # that command's stderr
    <name>/
      trajectory.json                 # `mngr transcript --format atif` (absent for a destroyed worker)
      common_transcript.jsonl         # the worker's stream, live or preserved
      reports/                        # the lead-side reports directory: report.md and consumed/
      transcript.err                  # the two commands' stderr
```

`context.metadata` gains:

- `workers`: one entry per launched worker, in launch order: `name`, `agent_id`, `agent_type`, `state` (`stopped`, `destroyed`, `running`, `unknown`), `depth`, `lead_name`, `launch_tool_call_id`, `document`, `stream`, and `report` (each `{"is_captured", "reason", "detail"}` like `transcript_capture`), and `usage` (the worker's own `workspace_usage_metadata`, or null when its stream was not read).
- `worker_launch_count` and `worker_captured_count` on `workspace_usage` and `transcript_usage`.
- `worker_capture_overflow`: the launches dropped by the round or count caps, if any.

No manifest entry, for the same reason the transcript capture has none: this is the trial's record, not outcome evidence, and a capture failure must not read to the outcome judge as an unmeasured check.

### What grading sees

Nothing changes at grade time.
`render_judge_transcript.py`, the gates, and the wordiness guard read the root document's `steps` and ignore `subagent_trajectories`, so worker conversations, which are addressed to the lead and never to the client, stay out of the judged transcript.
`harbor view` shows the embedded workers under their launching calls.

## Failure modes

| Situation | Recorded as | Effect |
|---|---|---|
| Launch scan finds no workers | nothing | the step does not run; no `workers/` directory |
| Worker stopped in place | `state: stopped`, both halves captured | embedded from its document |
| Worker destroyed after finishing | `state: destroyed`, document `transcript_command_failed`, stream captured from the preserved directory | embedded from a document built host-side from the stream |
| Worker destroyed and not preserved (preservation is best-effort) | `state: unknown` (nothing tells it from a name that never resolved), both halves `transcript_command_failed` | ref omitted; listed in metadata; `is_cost_complete` false |
| Name launched twice | one entry, under the first launch's call | one capture, of the agent answering to the name |
| Name never resolves (typo in the command, launch failed before `mngr create`) | both halves failed, `state: unknown` | ref omitted; the launching call's own result already shows the failure |
| More than 100 workers or more than 3 nesting rounds | `worker_capture_overflow` | the excess is not captured |
| Launch names no task file | `report: not_attempted` with the reason | trajectory captured, no report |
| Task file names no `finish_report_path`, or is not there | `report: no_report_path` | trajectory captured, no report |
| Reports directory missing (the worker never reported, or the lead deleted it) | `report: transcript_command_failed` with the copy's stderr | trajectory captured, no report |
| Bridge or budget failure on the listing exec | logged; each worker is captured by name and records its own outcome, `state: unknown` unless a preserved directory names it | streams may still be captured; ids come from preserved names or stay empty; no lead work dir, so a nested worker's report is not found |
| Rsync or download of the directory fails | every worker of the trial `pull_failed`/`download_failed` | nothing embedded; nothing priced |
| A worker document fails harbor validation host-side | recorded on the worker | rebuilt from the stream; if that fails too, ref omitted |
| Root document is hand-built | n/a | workers captured and priced, not embedded |

Every failure is recorded on the worker's metadata entry and never raised.

## Alternatives considered

- **Teach mngr to resolve workers** (`build_trajectory_for_agent` matching a `mngr_worker_parent_id` label and joining on `--name` in Bash commands).
  Every `mngr transcript` consumer would benefit, but it needs a workspace-template change to stamp the label, a `libs/mngr` change that puts a launch-task heuristic into core mngr, a pin bump, and it still cannot see destroyed workers.
  Worth doing later; the name-based discovery here keeps working if it lands.
- **Rebuild the whole root document host-side** from the captured streams with mngr's pure builder, passing workers as `subagent_by_call_id`.
  It reuses mngr's merge rules for the refs, but it would also have to re-resolve proxy subagents from labels, duplicating `api/trajectory.py`; grafting onto the document the workspace already built is smaller.
- **Sidecars only**, no embedding.
  A day less work, but `harbor view` and every ATIF consumer would still see a trial that stops at the launch.
- **Discovery from the agent listing** (`agent_created=true`).
  Sweeps in automations and the caretaker, misses destroyed workers, and gives no link to the launching step.

## Testing

- `trajectory_test.py`: the launch scan over a stream with `launch`, `launch-sync`, `mngr create`, `await`, and `destroy` calls; grafting onto the root document (ref on the right result, `subagent_kind`, nesting, validation), the synthesized placeholder when a result is missing, and a destroyed worker's document built from its stream. The hand-built root needs no test of its own: `build_hand_built_trajectory` takes no workers.
- `usage_test.py`: `combine_trial_usages` per model, `is_cost_complete` against launched-versus-captured, and the proxy account unchanged.
- `evidence_collection_test.py`: the listing and per-worker command shapes; a scripted workspace captures a worker stopped in place (the directory pulled once, each part recorded) and, in another trial, a destroyed one from its preserved stream; a worker captured by name when the listing fails; a failed pull, download, or report copy recorded per part; the budget; the count and round caps.
- `driver_test.py`: end to end with a chat stream that launches a worker and a scripted worker document: `trajectory.json` in the box embeds it under the launching call, the transcript account includes its tokens and is complete, and the metadata lists it.
- The grade-time scripts need no new tests: they ignore `subagent_trajectories` by construction, which one renderer test pins.

## Effort

About two to three days: half a day for discovery and the capture/transfer step in the collector, one day for the reconciliation and its tests, half a day for usage, half a day for the driver wiring, docs, and the changelog.
