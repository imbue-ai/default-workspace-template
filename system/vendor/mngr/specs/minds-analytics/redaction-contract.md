# Transcript redaction contract

The exact field dispositions for common-transcript records collected from
explorer workspaces. Redaction runs **inside the workspace container**; the
collection runner receives only the redacted stream and treats it as
untrusted input (it validates and size-caps, it never remediates content).

This contract is versioned by the collection script's version hash (the
sha256 of the injected file set), which is stamped on every collected row and
every audit record. Materially loosening this contract requires updating this
document in the same PR.

## Input

Common-transcript JSONL records
(`$MNGR_AGENT_STATE_DIR/events/<agent_type>/common_transcript/events.jsonl`),
in either of the two stream vintages the fleet carries:

- **ATIF-shaped records** (see
  [`../atif-transcript-alignment/spec.md`](../atif-transcript-alignment/spec.md)):
  `header`, `step`, `observation`, framed by `type`/`event_id`/`emitter` plus a
  `timestamp` on everything except the header.
- **Legacy records**, still emitted by agents provisioned before the ATIF
  cutover (an agent keeps its emitter for life): `user_message`,
  `assistant_message`, `tool_result`, each with the
  `timestamp`/`event_id`/`source` envelope.

Note the envelope rename: what the legacy records called `source` (the emitting
script, e.g. `claude/common_transcript`) is `emitter` on the ATIF records, whose
own `source` is the ATIF step originator (`system`/`user`/`agent`).

## ATIF-record dispositions

| Field | Disposition |
|---|---|
| Envelope (`type`, `event_id`, `emitter`, `timestamp`) | Kept verbatim |
| `header.schema_version` | Kept verbatim (the whole header is envelope; it carries nothing else) |
| `step.source` (`system`/`user`/`agent`) | Kept verbatim |
| `step.message` | Kept after text scrubbing (below) |
| `step.reasoning_content` | **Dropped entirely** |
| `step.model_name`, `llm_call_count`, `is_copied_context`, `reasoning_effort` | Kept verbatim |
| `step.metrics` | Dropped except the numeric `prompt_tokens`, `completion_tokens`, `cached_tokens`, `cost_usd`, and `extra.cache_creation_input_tokens` |
| `step.metrics.prompt_token_ids`, `.completion_token_ids`, `.logprobs` | **Dropped entirely** (they are the transcript itself, detokenizable; no emitter sets them, and the allowlist above is what keeps one that starts to from shipping them) |
| `step.tool_calls[]` | The key is present only on steps that carry calls; `tool_call_id` and `function_name` kept verbatim |
| `step.tool_calls[].arguments` | **Dropped entirely** |
| `step.observation` (inline, on system steps) | Stripped exactly like an `observation` record's `results` (below) |
| `step.extra` | Dropped except `finish_reason`, `message_id`, `conversation_id`, `session_id`, `agent_id` (the collection-added one), `is_sidechain`, and `context_management` |
| `step.extra.is_sidechain` | Kept, coerced to a bool. Available but unused: the gold tables do not yet separate the sidechain lane from the main one |
| `step.extra.context_management` | Dropped except `type` and `boundary`, each coerced to a string |
| `observation.results[].source_call_id` | Kept verbatim |
| `observation.results[].extra.is_error`, `.extra.tool_name` | Kept, coerced, in place under `extra` |
| `observation.results[].extra.is_sidechain` | Kept, coerced to a bool, when the emitter set it (same availability note as the step's) |
| `observation.results[].content` | **Dropped entirely** (replaced by a sibling `content_byte_count`) |
| Emitter-specific extra fields | Dropped unless explicitly allowlisted (the same allowlist as below, plus the collection-added `agent_id`) |

A `step` without a `source`, and any record missing an envelope field, is
dropped and counted -- the same fail-closed rule as an unknown record type.

The redacted `header` survives the strip but is not stored as a row: it has no
event timestamp (it describes the stream, not an event in it) and its
`event_id` is the same on every agent's stream. The runner skips it by type
before its envelope check, so it does not land in the dropped-line count --
that count is the ops signal for corrupt or hostile output, and stream framing
is neither. Nothing is lost: the stream's vintage is evident from the record
types themselves.

## Legacy-record dispositions

| Field | Disposition |
|---|---|
| Envelope (`timestamp`, `event_id`, `source`, `type`) | Kept verbatim |
| Structural metadata (`role`, `parts_ordered`) | Kept verbatim |
| `user_message.content` | Kept after text scrubbing (below) |
| `assistant_message.text` | Kept after text scrubbing (below) |
| `assistant_message.model`, `finish_reason` | Kept verbatim |
| `assistant_message.usage` | Dropped except the numeric `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` |
| `assistant_message.tool_calls[].tool_name` | Kept verbatim |
| `assistant_message.tool_calls[].tool_call_id` | Kept verbatim |
| `assistant_message.tool_calls[].input_preview` | **Dropped entirely** |
| `parts[]` `text` parts | Kept after text scrubbing |
| `parts[]` `tool_call` parts | Tool name + id kept; arguments **dropped** |
| `parts[]` `tool_call_response` parts | **Dropped entirely** (a stub with the tool_call_id and an `is_error` flag survives) |
| `parts[]` `reasoning` parts | **Dropped entirely** |
| `tool_result.output` | **Dropped entirely** (record survives as tool_call_id + tool_name + `is_error` + output byte count) |
| Emitter-specific extra fields | Dropped unless explicitly allowlisted (`session_id`, `conversation_id`, and `message_id` are allowlisted, plus the collection-added `agent_id` naming the agent state directory the record came from) |

Rationale: tool inputs and outputs are where file contents, command output,
and paths live -- the overwhelming bulk of sensitive material. Dropping them
wholesale is simpler to reason about, simpler to audit, and cheaper than
scrubbing them. Product questions about tool behavior are answered from
names, counts, timings, and error rates.

## Text scrubbing (message text only)

Applied in order, inside the container, to `step.message` and to the legacy
`user_message.content` / `assistant_message.text` / text parts. Where ATIF
allows a list of content parts instead of a string (no emitter of ours writes
one), the list is serialized to JSON and scrubbed as text -- degraded, but
never passed through unscanned:

1. **Secret scanning**: the workspace's pinned secret scanners (betterleaks
   and kingfisher, already installed in every workspace image) run over the
   text; any finding's span is replaced with `[REDACTED_SECRET]`.
2. **PII removal**: Presidio (installed on first collection via `uv` inside
   the workspace, pinned in the injected script) replaces detected entities
   (email addresses, phone numbers, person names, physical addresses, IP
   addresses, credit cards) with `[REDACTED_<ENTITY_TYPE>]`.
3. **Random-token scrubbing**: identifier-shaped, random-looking tokens --
   UUIDs, hex runs of 16+, digit runs of 7+, and high-entropy token shapes
   (length, character-class alternation, and Shannon-entropy thresholds) --
   are replaced with `[REDACTED_TOKEN]`. Transcripts are collected for
   reading the words, so this step is deliberately aggressive. Two carve-outs:
   workspace-local paths (chunks starting `/home/user` or `~/`) are kept
   whole, and other path-like strings are scrubbed per `/`-segment so their
   readable parts survive.

## What the runner enforces (outside, on untrusted output)

- Per-line and per-run size caps; oversize or schema-invalid lines are
  dropped and counted, never parsed further. The ATIF stream header is the one
  line skipped without being counted (see above).
- Envelope validation only (timestamp/event_id/type shape, plus the emitting
  source -- `emitter` on ATIF records, `source` on legacy ones); the runner
  never inspects or transforms message text.
- Every stored row is stamped with the collecting script's version hash, the
  run id, the workspace host id, and the account id.
