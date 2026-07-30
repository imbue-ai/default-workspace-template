/**
 * Claude Code slash commands the chat cannot deliver to an agent.
 *
 * A chat message reaches an agent by being typed into its terminal. The commands listed here
 * replace Claude Code's input box with a full-pane view, so the agent is left occupied by that
 * view and cannot accept any further message until someone dismisses it in the terminal -- which
 * a chat user has no way to do. Sending one is therefore refused in the composer.
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
 */

export const INPUT_BLOCKING_SLASH_COMMANDS: readonly string[] = ["/status"];

/**
 * The input-blocking command this message would run, or null if it would not run one.
 *
 * Matched on the command name only, so an argument does not slip the command through: Claude
 * ignores trailing text for these commands and opens the view regardless. This is deliberately
 * more conservative than the exact-match interception used for `/login` and `/logout`, where an
 * argument genuinely changes what the command does.
 */
export function findInputBlockingSlashCommand(text: string): string | null {
  const firstToken = text.trim().toLowerCase().split(/\s+/, 1)[0] ?? "";
  return INPUT_BLOCKING_SLASH_COMMANDS.find((command) => command === firstToken) ?? null;
}
