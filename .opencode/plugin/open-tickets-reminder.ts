import { type Plugin } from "@opencode-ai/plugin"

// experimental.chat.messages.transform (opencode): substitute for
// claude_open_tickets_reminder.sh's UserPromptSubmit. opencode has no
// UserPromptSubmit-equivalent hook with a real content-injection channel --
// the first candidate checked, chat.message, only exposes `message:
// UserMessage` with no `parts` array to mutate, so it can't actually inject
// text. experimental.chat.messages.transform is the real candidate: its
// output gives the full message list, each with a mutable `parts` array.
//
// Caveats, stated plainly rather than assumed away:
// - This hook is explicitly under opencode's "experimental" namespace --
//   real risk of the API shape changing in a future version.
// - Its exact firing frequency relative to a single user turn is NOT
//   confirmed (it plausibly fires once per model call within a turn, as
//   tool calls proceed, not once per user submission like claude's hook) --
//   so this guards against repeat-injecting into the SAME user message via
//   an in-memory per-message-id Set, rather than assuming exactly-once
//   semantics it hasn't been shown to have.
export const OpenTicketsReminderPlugin: Plugin = async ({ $, directory }) => {
  const remindedMessageIds = new Set<string>()

  return {
    "experimental.chat.messages.transform": async (_input, output) => {
      const last = output.messages[output.messages.length - 1]
      if (!last || last.info.role !== "user") return
      if (remindedMessageIds.has(last.info.id)) return

      // `||`, not `??` -- see require-steps.ts's identical fix for why.
      const ticketsDir = process.env.TICKETS_DIR || `${directory}/.tickets`
      const tkScript = `${directory}/vendor/tk/ticket`

      const dirCheck = await $`test -d ${ticketsDir}`.quiet().nothrow()
      if (dirCheck.exitCode !== 0) return
      const execCheck = await $`test -x ${tkScript}`.quiet().nothrow()
      if (execCheck.exitCode !== 0) return

      const env = { ...process.env, TICKETS_DIR: ticketsDir }
      const openLines = (await $`${tkScript} steps`.env(env).quiet().nothrow()).stdout
        .toString()
        .split("\n")
        .filter((line) => line.trim().length > 0)
      if (openLines.length === 0) return

      remindedMessageIds.add(last.info.id)

      const text = [
        "[Open task reminder from forever-claude-template]",
        "",
        "You have step records that are not yet closed:",
        "",
        ...openLines,
        "",
        'For each one, decide before continuing: keep working on it (call `tk start <id>` if it\'s not already in_progress), replace it with a fresh step, or close it now with `tk close <id> "<summary>"` (the positional summary is required for steps). Steps are sequential: do not start a new step until the previous one is closed.',
        "",
        "See AGENTS.md > Task management for the full protocol.",
      ].join("\n")

      last.parts.push({
        id: `synthetic-tk-reminder-${last.info.id}`,
        sessionID: last.info.sessionID,
        messageID: last.info.id,
        type: "text",
        text,
        synthetic: true,
      })
    },
  }
}
