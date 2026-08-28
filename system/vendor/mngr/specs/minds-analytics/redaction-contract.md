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
per the common-transcript standard: `user_message`, `assistant_message`,
`tool_result`, each with the `timestamp`/`event_id`/`source` envelope.

## Dispositions

| Field | Disposition |
|---|---|
| Envelope (`timestamp`, `event_id`, `source`, `type`) | Kept verbatim |
| Structural metadata (`role`, `parts_ordered`) | Kept verbatim |
| `user_message.content` | Kept after text scrubbing (below) |
| `assistant_message.text` | Kept after text scrubbing (below) |
| `assistant_message.model`, `usage`, `finish_reason` | Kept verbatim |
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

Applied in order, inside the container, to `user_message.content` and
`assistant_message.text` / text parts:

1. **Secret scanning**: the workspace's pinned secret scanners (betterleaks
   and kingfisher, already installed in every workspace image) run over the
   text; any finding's span is replaced with `[REDACTED_SECRET]`.
2. **PII removal**: Presidio (installed on first collection via `uv` inside
   the workspace, pinned in the injected script) replaces detected entities
   (email addresses, phone numbers, person names, physical addresses, IP
   addresses, credit cards) with `[REDACTED_<ENTITY_TYPE>]`.

## What the runner enforces (outside, on untrusted output)

- Per-line and per-run size caps; oversize or schema-invalid lines are
  dropped and counted, never parsed further.
- Envelope validation only (timestamp/event_id/type/source shape); the
  runner never inspects or transforms message text.
- Every stored row is stamped with the collecting script's version hash, the
  run id, the workspace host id, and the account id.
