Workspace usage summarization now reads both common-transcript vintages: the
workspace system_interface's legacy `assistant_message` records and the
ATIF-shaped agent `step` records mngr's own emitters write.

The token reconciliation is the part worth knowing about. ATIF's
`metrics.prompt_tokens` is cache-*inclusive* (every input token, cached or
not), while the legacy `input_tokens` excluded cache reads and writes and is
what the pricing buckets expect. The ATIF counts are therefore converted on the
way in -- `input = prompt_tokens - cached_tokens - extra.cache_creation_input_tokens`
-- so the two vintages describing the same response produce the same cost
instead of double-counting cached tokens at the full input rate.

Delegation detection understands ATIF tool calls too: the Agent/Task check
reads `function_name`, and the worker-launch markers are matched against the
serialized `arguments` object -- which, being complete rather than a 200-char
preview, now catches launch commands the preview used to truncate away.

The other readers of the raw workspace event stream move with it: the driver's
agent-reply detection (which decides when a turn finished) and the grade-time
judge-transcript renderer both understand ATIF `step` records. In that vintage
the framework noise the legacy shape flagged with `is_meta` arrives as a
`system` step, which is dropped for not being a client turn at all. The decider
is unaffected -- it reads the driver's own rendered conversation, not the raw
stream.
