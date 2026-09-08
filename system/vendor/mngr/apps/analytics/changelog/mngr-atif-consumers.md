Transcript redaction now handles the ATIF-shaped common-transcript records
(`header`, `step`, `observation`) alongside the legacy ones, so workspaces
whose agents were provisioned after the ATIF cutover keep being collected from
with the same guarantees.

The new dispositions, recorded in `specs/minds-analytics/redaction-contract.md`:
a step's `message` is kept and scrubbed like the legacy message text, its
`reasoning_content` is dropped outright, its tool calls keep only the call id
and function name (the full `arguments` object is dropped, and the key is
absent entirely on steps that made no call), and its `extra` is reduced to an
allowlist (`finish_reason`, `message_id`, `conversation_id`, `session_id`, the
sidechain marker, and a `context_management` descriptor cut down to its `type`
and `boundary`). Observation results keep their `source_call_id` plus
`extra.is_error`/`extra.tool_name`/`extra.is_sidechain` and replace the tool
output with a `content_byte_count`. Unknown record types are still dropped and
counted.

Token counters are allowlisted rather than passed through: a step's `metrics`
keeps `prompt_tokens`, `completion_tokens`, `cached_tokens`, `cost_usd`, and
`extra.cache_creation_input_tokens` when each is a number, and the legacy
`usage` block keeps its four counters the same way. That is what structurally
keeps ATIF's `prompt_token_ids` / `completion_token_ids` / `logprobs` fields out
of the lake -- no emitter sets them today, and one that started to would
otherwise ship a detokenizable copy of the transcript through a stage that only
claims to carry counts.

The claude emitter's sidechain marker now reaches the lake on both steps and
observation results. Nothing queries it yet: separating the sidechain lane from
the main one in the gold tables is still future work.

The collection runner's envelope validation now reads the emitting source from
the ATIF `emitter` field (which is what the legacy `source` field meant --
ATIF's own `source` names the step originator), so observation and step records
survive validation. The stream `header` is not stored: it carries no event
timestamp, and its event id is identical on every agent's stream. It is skipped
by type before the envelope check, so it does not land in the run's
dropped-line count -- that count is the ops signal for corrupt or hostile
script output, and stream framing is neither.

The gold-table derivations follow: `transcript_daily`, `transcript_tools_daily`,
and the `workspace_user_message` activity signal now count turns and tool
results across both vintages. An ATIF `step` discriminated by its `source` is
counted where a legacy `user_message`/`assistant_message` record was, and each
entry of an `observation` record's `results[]` is counted where a legacy
`tool_result` record was (tool names and error flags read from
`results[].extra`). A system step's inline observation is not counted as a tool
result -- it carries compaction output, not a tool call's.
