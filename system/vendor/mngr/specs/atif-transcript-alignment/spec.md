# Common transcript: alignment with the ATIF standard

**Audience:** developers working on the `mngr` common-transcript schema, the per-agent
emitters (claude, codex, antigravity, opencode, pi-coding), and the transcript consumers
(`mngr transcript`, analytics collection, minds_evals usage, preservation).

**Status:** implemented. Supersedes
[`../common-transcript-standard/spec.md`](../common-transcript-standard/spec.md) (OTel GenAI
vocabulary alignment): that spec concluded "no published on-disk standard for an agent
transcript session file exists", which is no longer true -- Harbor's **Agent Trajectory
Interchange Format (ATIF)** is exactly that standard, and this spec aligns the common
transcript with it.

This spec redefines the agent-agnostic *common transcript* as a streaming JSONL form of
ATIF (Harbor's Agent Trajectory Interchange Format, v1.7), plus a doc-builder in `mngr`
that assembles the stream into a valid single-document ATIF trajectory for interop with
ATIF tooling (viewers, SFT/RL pipelines, harbor-based evals).

Related:

- ATIF RFC: `rfcs/0001-trajectory-format.md` in the harbor repo (v1.7)
- Harbor reference models: `src/harbor/models/trajectories/` in the harbor repo
- Current schema: `libs/mngr/imbue/mngr/agents/common_transcript_records.py`
- Reader: `libs/mngr/imbue/mngr/cli/transcript.py`
- Mixins: `HasTranscriptMixin` / `HasCommonTranscriptMixin` in `libs/mngr/imbue/mngr/interfaces/agent.py`
- Provisioning: `libs/mngr/imbue/mngr/agents/common_transcript.py`, `libs/mngr/imbue/mngr/resources/mngr_common_transcript_lib.sh`
- Superseded spec: [`../common-transcript-standard/spec.md`](../common-transcript-standard/spec.md)
- Cross-plugin feature state: [`../agent-plugin-parity/spec.md`](../agent-plugin-parity/spec.md)

## Contents

- [Background](#background)
- [Why ATIF, and why now](#why-atif-and-why-now)
- [Goals and non-goals](#goals-and-non-goals)
- [Design overview](#design-overview)
- [Stream format](#stream-format)
- [Building the full document](#building-the-full-document)
- [Vendored ATIF models](#vendored-atif-models)
- [Per-agent emitter notes](#per-agent-emitter-notes)
- [Consumers and migration](#consumers-and-migration)
- [Compatibility](#compatibility)
- [Testing and conformance](#testing-and-conformance)
- [Open questions](#open-questions)
- [References](#references)

## Background

Every agent plugin emits an agent-agnostic transcript stream at
`$MNGR_AGENT_STATE_DIR/events/<agent_type>/common_transcript/events.jsonl`. Today each line
is one of three bespoke record types (`user_message`, `assistant_message`, `tool_result`)
sharing an envelope (`timestamp`, `event_id`, `source`), with OTel-aligned vocabulary
(`finish_reason`, ordered `parts[]`). Emitters run *on the agent's host* with only the
stdlib available (Python for claude/codex/antigravity, TypeScript inside the agent's plugin
runtime for opencode/pi-coding), append incrementally (a 5s daemon plus turn-end flushes,
serialized by a lock in `mngr_common_transcript_lib.sh`), and dedup by source-derived
`event_id` so re-processing never duplicates output.

The current format is deliberately lossy: tool inputs are stored as 200-char previews,
tool outputs truncated at 2000 chars, and reasoning/thinking content is dropped. That
lossiness was acceptable for display but blocks the new consumers we care about:
harbor-style eval tooling, SFT/RL pipelines, and any external ATIF consumer.

## Why ATIF, and why now

ATIF is a JSON-based specification for logging complete agent interaction histories,
maintained in the harbor repo and used as harbor's standard trajectory format. It defines a
root object (schema version, agent identity, aggregate metrics) over a sequential `steps[]`
array, where each step is a system prompt, a user message, or a complete agent turn
(message, reasoning, tool calls, observation results, per-step metrics). v1.7 adds embedded
subagent trajectories and a context-management convention for compaction.

Aligning buys us, in order of importance:

1. **Interop:** mngr transcripts become consumable by ATIF tooling (harbor validators and
   viewers, SFT/RL pipelines, minds_evals) without bespoke adapters.
2. **Less bespoke vocabulary:** field names and message shapes come from a maintained
   external spec instead of our own invention (the same motivation as the superseded OTel
   spec, but now for the whole format rather than just names).
3. **Full fidelity:** adopting ATIF's required `arguments` object forces the fidelity
   upgrade (full tool inputs/outputs, reasoning) that downstream training/eval use cases
   need anyway.

**What does not fit directly:** ATIF is a single JSON document with sequential `step_id`s
and observations attached to the step that issued the tool calls. Our transcript must be
written *incrementally on the host* (append-only, crash-tolerant, cheap to tail), and tool
results arrive after the assistant message that called them. The design therefore keeps a
JSONL stream as the on-disk source of truth -- with records that use ATIF's vocabulary and
sub-schemas -- and moves document assembly into `mngr` proper, which has the full picture.

## Goals and non-goals

**Goals**

- Stream records use ATIF vocabulary and sub-schemas (`ToolCallSchema`, `MetricsSchema`,
  `ObservationResultSchema`, step `source` values) verbatim wherever a concept maps 1:1.
- Full fidelity always: complete `arguments` objects, untruncated outputs, and
  `reasoning_content` wherever the native source has it. No truncation mode, no fidelity
  flag. Display truncation moves to the reader.
- A doc-builder in `libs/mngr` (library API + CLI surface) assembles a stream into a valid
  single-document ATIF trajectory, including embedded claude subagent trajectories.
- Built documents validate against the vendored harbor ATIF models (pinned to ATIF-v1.7).
- All five emitters convert in this effort; consumers migrate in the same effort.

**Non-goals**

- No on-disk compatibility with the current record types; no read-side shim (see
  [Compatibility](#compatibility)).
- No multimodal image capture (ATIF v1.6 `ContentPart` images). Emitters that encounter
  images emit a text placeholder (e.g. `[image omitted]`); the `images/` sidecar convention
  is deferred.
- No redaction in the emitters or the stream. Raw native transcripts already sit unredacted
  on the same host, so the stream adds no new exposure; redaction remains a downstream
  consumer concern (analytics `workspace_redaction`, any future exporter).
- No `tool_definitions` capture (ATIF v1.5): no native source exposes the tool schemas to
  our emitters today.
- No continuation-file splitting (`continued_trajectory_ref`): one agent has one stream for
  its whole life; compaction is represented in-band as a system step.

## Design overview

Three layers, replacing the current two:

| Layer | Where it runs | What it produces |
|---|---|---|
| **Emitters** (five) | On the agent's host, stdlib-only / TS plugin | Append-only JSONL stream of ATIF-shaped records |
| **Doc-builder** (new) | In `mngr` (full Python env, vendored ATIF models) | Valid single-document ATIF trajectory |
| **Readers** | In `mngr` / downstream | Human display (`mngr transcript`), analytics feeds, eval tooling |

The stream stays at `events/<agent_type>/common_transcript/events.jsonl` and stays
append-only, so analytics' byte-offset cursors keep working structurally (the record
schema changes; the container does not).

## Stream format

### Record framing

Each line is a JSON object with three **framing fields** that belong to mngr's stream
container, not to ATIF:

| Field | Purpose |
|---|---|
| `type` | Record discriminator: `header`, `step`, or `observation`. |
| `event_id` | Source-derived idempotency key (same derivation rules as today); emitters skip records whose id already exists in the output. |
| `emitter` | The emitting source, e.g. `claude/common_transcript` (renamed from today's `source`, which ATIF claims for step originator). |

All remaining fields on a record are ATIF fields. The doc-builder strips the framing fields
when assembling the document (preserving `event_id`/`emitter` under `step.extra` so
provenance survives into the built form).

**Note:** the envelope rename (`source` -> `emitter`) is forced: ATIF's `StepObject.source`
means "system | user | agent" and the two cannot share a name.

### `header` records

The first line of every stream. Minimal by design -- the doc-builder fills everything else
from mngr's own records at build time:

```json
{"type": "header", "event_id": "header-<sha256(agent_id:emitter)[:32]>", "emitter": "claude/common_transcript", "schema_version": "ATIF-v1.7"}
```

The header's `event_id` hashes the agent id (the agent state directory's basename, a
UUID4-based value) and the emitter: a fixed id would repeat identically for every agent
on every host, and analytics' fleet-wide event-id dedupe would collapse all header rows
to one, destroying the per-agent (emitter, `schema_version`) mix.

`schema_version` pins which ATIF revision the stream's records follow, so a future ATIF
bump is detectable per-stream. Emitters write the header on first append (creation of the
file), guarded by the same convert lock as all appends.

### `step` records

One per ATIF step, carrying the `StepObject` fields except `step_id` (assigned at build
time) and `observation` (streamed separately for agent steps; see below):

- `source: "user"` -- real user input. `message` is the full text.
- `source: "agent"` -- one assistant turn: `message` (assistant text), `reasoning_content`
  (thinking, where the native source has it), `tool_calls[]` (full ATIF `ToolCallSchema`:
  `tool_call_id`, `function_name`, `arguments` as the complete JSON object), `model_name`,
  `metrics` (ATIF `MetricsSchema` names: `prompt_tokens`, `completion_tokens`,
  `cached_tokens`, provider extras such as cache-write counts under `metrics.extra`), and
  `finish_reason` under `extra` (ATIF has no stop-reason field; we keep it as a step-level
  extra).
- `source: "system"` -- framework-injected messages (claude's `isMeta` records, stop-hook
  output) and system-initiated operations. Compaction/summarization events carry the ATIF
  v1.7 `context_management` convention in `extra`
  (`{"context_management": {"type": "compaction", "boundary": "replace"}}`) with the
  summary in an inline `observation`. System steps that already have their result at
  emission time carry `observation` inline on the step record -- there is no async gap to
  bridge for them.

`timestamp` is the ATIF step timestamp and doubles as the stream ordering aid.

The current `parts[]`/`parts_ordered` representation is dropped from the required schema.
ATIF has no interleaving concept; a step's `message` is the concatenated assistant text and
`tool_calls[]` is the ordered call list. Where an emitter previously split one native
assistant message into ordered text/tool_call parts, it now emits one `step` record per LLM
inference following ATIF's one-LLM-per-step convention, and interleaving fidelity within a
single inference is not represented in the core fields. Reintroducing ordered parts under
`step.extra` (e.g. `extra.parts` with the old `parts_ordered` flag) is deferred as optional
future work for emitters with faithful ordering (see [Open questions](#open-questions)).

### `observation` records

Tool results for agent steps, streamed as they arrive:

```json
{"type": "observation", "event_id": "<uuid>-tool_result-<call_id>", "emitter": "claude/common_transcript",
 "timestamp": "...", "results": [
   {"source_call_id": "toolu_abc", "content": "<full untruncated output>",
    "extra": {"is_error": false, "tool_name": "Bash"}}]}
```

`results[]` entries are ATIF `ObservationResultSchema` objects. `is_error` and `tool_name`
have no ATIF field, so they live in `result.extra` (the reader uses both for display).
Every result from a tool call MUST carry `source_call_id`; emitters do not emit
call-id-less observation records (system-initiated results ride inline on their system
step instead, per above).

### Fidelity rules

- `arguments` is always the complete parsed JSON object from the native source. If the
  native source only has a serialized string that fails to parse, the emitter wraps it as
  `{"_raw": "<string>"}` rather than dropping it.
- Observation `content` is the full output text, untruncated.
- `reasoning_content` is captured wherever the native transcript exposes it (see the
  per-agent table). It is plain text; multiple thinking blocks in one inference are
  joined with blank lines.
- Images anywhere (user messages, tool results) become the text placeholder
  `[image omitted]` in the text extraction.

## Building the full document

### Library API and CLI

A new module in `libs/mngr` (e.g. `imbue/mngr/agents/trajectory_build.py`) exposes:

- `build_trajectory(...) -> Trajectory`: reads a stream (via the existing events API /
  `HostInterface` so it works for remote hosts), merges records into a validated ATIF
  `Trajectory` (vendored models), and enriches the root.
- CLI surface: `mngr transcript <agent> --format atif`, writing the built document to
  stdout (or `--output <path>`). The existing text/json display modes remain the default.

### Merge rules

1. Records are processed in file order (append order is authoritative; timestamps are not
   used for reordering).
2. The `header` supplies `schema_version`. A missing or non-first header is a build error.
3. `step` records become `steps[]` entries in order; `step_id` is assigned sequentially
   from 1.
4. Each `observation` result attaches to the step whose `tool_calls[]` contains its
   `source_call_id`, merged into that step's `observation.results[]` in arrival order. A
   result whose `source_call_id` matches no step is a build warning and is preserved on the
   nearest preceding agent step with `extra.unmatched: true` (never silently dropped).
5. Root enrichment from mngr's own records (not from the stream): `agent.name` (agent
   type), `agent.version` (the plugin/CLI version recorded in agent `data.json` where
   available, else `"unknown"` -- ATIF requires the field), `session_id` (the mngr agent
   id), `trajectory_id` (a stable per-build identifier derived from the agent id), and
   `final_metrics` (sums of the per-step metrics).
6. Validation: the assembled document is validated by the vendored `Trajectory` model
   before being returned/written; failures are build errors, not silent output.

### Subagent embedding

Two kinds of subagent exist, and built documents distinguish them via an `extra` field on
both the ref and the embedded trajectory: `extra.subagent_kind`, valued `"mngr"` or
`"native"`.

**mngr subagents** (`subagent_kind: "mngr"`): claude subagents that mngr runs as sibling
proxy agents (`mngr_claude_subagent_proxy`) with their own streams. At build time, for each
Task-style tool call whose subagent the proxy plugin can resolve to a sibling agent, the
doc-builder:

1. Recursively builds the subagent's trajectory, assigns it a `trajectory_id`, sets
   `extra.subagent_kind: "mngr"` on it, and appends it to the parent's
   `subagent_trajectories[]` (ATIF v1.7 embedded form).
2. Attaches
   `{"subagent_trajectory_ref": [{"trajectory_id": "...", "extra": {"subagent_kind": "mngr"}}]}`
   to the observation result of the delegating tool call, keeping the textual result in
   `content` as the quick-reference summary.

**Native subagents** (`subagent_kind: "native"`): subagents the CLI runs inside its own
session (e.g. claude Task agents recorded as sidechains in the native transcript, not
proxied by mngr). Where the emitter or doc-builder can carve their events out into a
separate trajectory, they are embedded the same way with `extra.subagent_kind: "native"`;
where it cannot, their delegating tool call keeps its plain textual observation result and
no ref is attached.

A subagent of either kind that cannot be resolved (destroyed, stream missing,
indistinguishable sidechain) leaves the plain textual observation result untouched.

## Vendored ATIF models

Harbor cannot be a workspace dependency (its dependency floors do not co-resolve with the
workspace; this is why `apps/minds_evals` is standalone). Harbor's ATIF pydantic models,
however, are ~10 self-contained files with no harbor-internal imports beyond each other.

- Vendor them one-time into `libs/mngr/imbue/mngr/agents/data_types/atif/` (inside the `imbue`
  package, so the published wheel stays self-contained), with a `README.md` recording the
  source repo, the harbor commit, and the pinned `schema_version` (`ATIF-v1.7`).
- Re-vendoring is a deliberate, manual act when we choose to adopt a newer ATIF revision;
  there is no sync automation.
- The emitters do NOT import these models (they are stdlib-only on-host scripts); the
  models serve the doc-builder, the record schema, and the conformance tests.

The stream-record schema (`common_transcript_records.py`, rewritten) defines the three
framing record types and composes the vendored sub-models (`ToolCall`, `Metrics`,
`ObservationResult`) so the ATIF payload shapes are stated exactly once.

## Per-agent emitter notes

All five emitters are rewritten against the RFC (harbor's converters are reference
material only -- they are methods buried in harbor agent classes and not vendorable).
Native sources for the new fidelity:

| Agent | Full `arguments` | `reasoning_content` | System steps | Notes |
|---|---|---|---|---|
| **claude** | `tool_use.input` (already full in raw events) | `thinking` blocks in `message.content[]` | `isMeta` records + compaction summaries | Today's fake `meta` tool_result reclassification is replaced by real system steps. |
| **codex** | `function_call.arguments` (JSON string; parsed, `_raw`-wrapped on parse failure) | reasoning items in the rollout where present | session-configured instructions where present | Assistant turns are text-only or single-call; each rollout item maps to one step record. |
| **antigravity** | decoded planner `tool_calls[]` args | `PLANNER_THINKING` (currently decoded then discarded) | injected framework messages | Order of calls within a turn remains best-effort; acceptable since ATIF does not model interleaving. |
| **opencode** | full part payloads from `message.part.updated` | reasoning parts where the plugin surfaces them | plugin-visible system events | TS plugin emits the new record shapes directly. |
| **pi-coding** | `toolCall` block args | thinking blocks where present | lifecycle-visible system events | TS lifecycle hook, same as opencode. |

Each emitter keeps its current dedup derivation (`event_id` from native uuids/ids) and its
current locking/flush structure; only the record payloads change.

## Consumers and migration

No compatibility layer. All consumers move to the new records in the same effort:

- **`mngr transcript` (reader)**: renders `step`/`observation` records; display-time
  truncation applies the old preview caps (200-char tool inputs, 2000-char outputs) as
  *rendering* behavior with a `--full` escape hatch. Role filtering maps to ATIF `source`
  values plus `observation` records (`--role user|agent|system|tool`). Adds
  `--format atif` (the doc-builder).
- **Analytics** (`apps/analytics` injected feeds): cursor mechanics unchanged
  (append-only JSONL, byte offsets). `workspace_feeds`/`workspace_redaction` update to the
  new record shapes; redaction now matters more since arguments/outputs are complete.
- **minds_evals usage** (`apps/minds_evals/imbue/minds_evals/usage.py`): token keys change
  from `input_tokens`/`output_tokens`/`cache_read_tokens`/`cache_write_tokens` to ATIF
  `metrics` names (`prompt_tokens`/`completion_tokens`/`cached_tokens` +
  `extra.cache_creation_input_tokens`).
- **Preservation** (`api/preservation.py`): path layout unchanged; nothing to do.

## Compatibility

- Old-format lines and new-format lines never mix in one stream in practice: agents keep
  the emitter they were provisioned with, so a pre-cutover agent keeps its old-format
  stream for life (the same accepted gap as the superseded spec's `parts[]` rollout).
- The reader detects an old-format stream by record type -- any record whose `type` is one
  of the retired pre-ATIF names (`user_message`, `assistant_message`, `tool_result`) is
  reported as an unsupported old-format stream, with a clear error naming the agent's
  emitter vintage and the raw transcript as the recourse. The doc-builder applies the same
  detection and additionally requires the `header` to be the stream's first record. Neither
  attempts to render or rebuild old records. No shim, no dual-path.
- `validate_common_transcript_record` and the conformance machinery carry over against the
  new schema; the contract remains enforced at emit time, with the reader tolerant at read
  time.

## Testing and conformance

- **Per-plugin conformance tests** (existing pattern): each plugin's
  `test_emitted_common_records_conform_to_canonical_schema` drives the real emitter over
  representative native fixtures and validates every emitted line against the new record
  schema. The meta-test (`common_transcript_conformance_meta_test.py`) continues to require
  one per plugin.
- **Fidelity assertions**: conformance fixtures include a large tool input and a large
  output, asserting they survive un-truncated, and a thinking/reasoning fixture per agent
  that has one.
- **Doc-builder unit tests**: merge rules (ordering, step_id assignment, observation
  attachment, unmatched-result warning path, missing-header error), root enrichment, and
  subagent embedding against fixture streams; every built document must pass vendored
  `Trajectory` validation.
- **Golden documents**: at least one end-to-end fixture stream per agent with a checked-in
  built ATIF document (inline-snapshot or golden file) so format drift is visible in
  review.
- **Old-format detection test**: a current-format fixture stream renders; an old-format
  fixture produces the unsupported error.

## Open questions

- **Observation linking**: this spec attaches results by `source_call_id` alone (build-time
  matching), with system results inlined on their step. The alternative -- observation
  records carrying the parent step's `event_id` -- removes the nearest-preceding fallback
  for unmatched results at the cost of emitter-side state. Adopted default: match by
  `source_call_id`; revisit only if real streams produce unmatched results.
- **Old-format streams**: adopted default is the clean unsupported error with no shim,
  matching the precedent from the superseded spec. If kanpan-style peeks at long-lived
  pre-cutover agents turn out to matter, a read-side shim behind a `# CLEANUP:` marker is
  the fallback.
- **`agent.version` sourcing**: ATIF requires it; whether agent `data.json` reliably has a
  CLI/plugin version for all five agent types needs verification during implementation
  (`"unknown"` is the specified fallback).
- **ATIF gaps we paper over with `extra`** (`finish_reason`, `is_error`, `tool_name` on
  results, `subagent_kind` on refs and embedded trajectories): worth proposing upstream to
  the ATIF maintainers; until then the `extra` keys are part of our documented contract.
- **Ordered `parts[]` under `extra`** (deferred optional work): emitters with faithful
  ordering (claude, pi-coding, opencode) could carry the superseded spec's ordered
  text/tool_call `parts[]` (plus `parts_ordered`) under `step.extra` to preserve
  intra-inference interleaving that ATIF's core fields cannot represent. Not part of this
  effort; nothing in the new format precludes adding it later.

## References

- ATIF RFC v1.7: `rfcs/0001-trajectory-format.md` (harbor repo)
- Harbor ATIF models: `src/harbor/models/trajectories/` (harbor repo)
- Harbor trajectory validator: `src/harbor/utils/trajectory_validator.py` (harbor repo)
- Superseded OTel-alignment spec: [`../common-transcript-standard/spec.md`](../common-transcript-standard/spec.md)
- OpenTelemetry GenAI semantic conventions (vocabulary source for the superseded spec):
  <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/>
