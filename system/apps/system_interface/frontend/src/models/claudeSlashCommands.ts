/**
 * Claude Code slash commands the chat declines to send to an agent.
 *
 * A chat message reaches an agent by being typed into its terminal, so a command that changes the
 * terminal rather than starting a turn does something the chat cannot undo. Most of these replace
 * Claude Code's input box with a full-pane view, after which the agent cannot accept any further
 * message until the view is dismissed; `/exit` (and its alias `/quit`) shuts the session down
 * outright. Either way the command still works from the agent's terminal, which is what the notice
 * points the user at.
 *
 * Which commands behave this way is a fact about Claude Code, not about the chat, so it lives in
 * its own module rather than inline in the composer.
 *
 * The composer applies this unconditionally, with no check of which kind of agent is on the other
 * end. That is safe only because this chat cannot show a non-Claude agent at all: every message it
 * renders comes from parsing Claude Code's own session transcript, and sign-in is handled by
 * Claude-specific auth code. If the chat ever gains a second agent type, this list stops being
 * universally correct -- another agent's slash commands are its own -- and the guard has to become
 * per-agent-type instead.
 *
 * Every entry was measured against claude 2.1.220 by sending it to a live agent and confirming both
 * that the input box was gone afterwards and that a following message failed to send. The command's
 * kind in Claude's own registry is NOT a reliable predictor and was not used to decide membership:
 * plenty of commands that render an interactive component (`/model`, `/plugin`, `/rewind`,
 * `/version`) leave the input box alone and send fine.
 *
 * Alias spellings sit alongside the command they resolve to, since a user can type either and
 * Claude treats them identically -- `/cost` and `/stats` are `/usage`, `/settings` is `/config`,
 * `/allowed-tools` is `/permissions`, `/bashes` is `/tasks`, `/quit` is `/exit`. Not duplicates.
 */

export const DECLINED_SLASH_COMMANDS: readonly string[] = [
  "/add-dir",
  "/allowed-tools",
  "/bashes",
  "/config",
  "/cost",
  "/diff",
  "/exit",
  "/extra-usage",
  "/goal",
  "/help",
  "/hooks",
  "/ide",
  "/mcp",
  "/permissions",
  "/powerup",
  "/privacy-settings",
  "/quit",
  "/release-notes",
  "/settings",
  "/skills",
  "/stats",
  "/status",
  "/tasks",
  // Declined on the strength of its argument form: bare `/theme` sends fine, but `/theme dark`
  // takes over the input box. Matching by name covers both.
  "/theme",
  "/usage",
  "/usage-credits",
  "/workflows",
];

/**
 * The declined command this message would run, or null if it would not run one.
 *
 * Matched on the command name only, so an argument does not slip the command through. This is
 * deliberately more conservative than the exact-match interception used for `/login` and `/logout`,
 * where an argument genuinely changes what the command does.
 */
export function findDeclinedSlashCommand(text: string): string | null {
  const firstToken = text.trim().toLowerCase().split(/\s+/, 1)[0] ?? "";
  return DECLINED_SLASH_COMMANDS.find((command) => command === firstToken) ?? null;
}
