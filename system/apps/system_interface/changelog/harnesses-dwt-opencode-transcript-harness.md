opencode chats now render their conversation. The opencode harness tails opencode's own
SQLite store (`opencode.db`) directly to reconstruct the transcript -- user and assistant
messages, tool calls with readable labels ("Running ls", "Reading foo.py"), and tool results
-- reaching pi parity. The working indicator now shows "Thinking" the instant a turn begins
(and "Tool running" while a tool executes), verified against a live agent. Reasoning/thinking
is hidden and token usage is surfaced, matching the other harnesses.
