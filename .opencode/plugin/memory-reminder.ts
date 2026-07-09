import { type Plugin } from "@opencode-ai/plugin"

// experimental.chat.messages.transform (opencode): combined substitute for
// claude_memory_reminder_sessionstart.sh + claude_memory_reminder_userpromptsubmit.sh.
// Same mechanism and same caveats as open-tickets-reminder.ts (experimental
// namespace, unconfirmed exactly-once firing, guarded via a per-message-id
// Set) -- see that file's header for the full explanation.
//
// The "search memory" text fires once per session (tracked via a
// per-sessionID Set, a true session-start concern); the "save to memory"
// text fires on every subsequent qualifying user message in that session,
// mirroring the recurring UserPromptSubmit reminder on the other harnesses.
export const MemoryReminderPlugin: Plugin = async () => {
  const remindedMessageIds = new Set<string>()
  const sessionsWithSearchReminder = new Set<string>()

  return {
    "experimental.chat.messages.transform": async (_input, output) => {
      const last = output.messages[output.messages.length - 1]
      if (!last || last.info.role !== "user") return
      if (remindedMessageIds.has(last.info.id)) return
      remindedMessageIds.add(last.info.id)

      const sessionID = last.info.sessionID
      const isFirstForSession = !sessionsWithSearchReminder.has(sessionID)
      if (isFirstForSession) sessionsWithSearchReminder.add(sessionID)

      const text = isFirstForSession
        ? [
            "[Memory reminder]",
            "",
            "Before starting work, search the shared memory MCP server (opencode.json's mcp.memory) for context relevant to this task -- call its search_nodes tool. Facts and decisions saved there may be relevant regardless of which harness saved them.",
            "",
            "See AGENTS.md > Memory for the full protocol.",
          ].join("\n")
        : [
            "[Memory reminder]",
            "",
            "If your previous turn surfaced any fact, decision, or piece of context worth remembering across sessions, persist it now via the shared memory MCP server (create_entities / add_observations) before moving on.",
            "",
            "See AGENTS.md > Memory for the full protocol.",
          ].join("\n")

      last.parts.push({
        id: `synthetic-memory-reminder-${last.info.id}`,
        sessionID: last.info.sessionID,
        messageID: last.info.id,
        type: "text",
        text,
        synthetic: true,
      })
    },
  }
}
