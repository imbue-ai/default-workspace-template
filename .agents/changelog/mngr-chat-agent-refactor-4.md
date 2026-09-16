Phase 4 of the chat-agent split: the template side of a chat moving to another harness.

- A new `handoff-summary` skill: the slash command the chat app sends the agent a chat is leaving, naming the file to write its summary to. The skill writes the summary (what the user is doing, what is done and in progress, decisions, files, branches and workers, unanswered questions, what the user is waiting for) and stops without replying to the user.

- A new `continue-chat` reference (`.agents/shared/references/continue-chat.md`): the template of the first message a successor agent receives, which the chat app fills in with the summary's path or its absence, the predecessors' names, ids, and state dirs, the lanes involved, and the user's message.

- `AGENTS.md` gains a section every agent reads, "Continuing a chat that moved to you": what `MINDS_CHAT_ID` differing from `MNGR_AGENT_ID` means, where to find the summary and the predecessors' transcripts, that open `tk` steps carry over, that predecessors are never touched, and that none of this is mentioned to the user unless asked.

- The `find-transcripts` skill explains chats that have run on several agents: the `chat_id` and `chat_seq` labels every member carries, the `archived-<seq>-<name>-<id>` name of a retired member, and the `mngr list --include` query that lists one chat's agents so their transcripts read in order.
