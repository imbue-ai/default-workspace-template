Codex messages and task timelines now keep their identities across the live and saved transcript.

Codex 0.154 saves user messages as `item_completed/UserMessage` with a `client_id` field. Reading the app-server spelling, `clientId`, from that file lost the correlation ID and displayed the live and saved copies twice. Both the older `user_message` format and the newer item format now preserve the live message ID.

Code-mode command results printed as JSON objects now expose their task titles and summaries just like plain stdout. The original output remains available unchanged on expansion. Task commands are recognized across code-mode batches and through `uv run`, including single-quoted arguments.

Only results paired with a recognized task command can change the timeline. Reading a helper's report or searching a transcript no longer creates phantom tasks or changes an existing task's title or status from quoted commands.
