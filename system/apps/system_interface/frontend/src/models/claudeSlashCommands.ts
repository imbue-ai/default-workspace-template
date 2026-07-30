/**
 * Claude Code slash commands the chat declines to send to an agent.
 *
 * A chat message reaches an agent by being typed into its terminal, so a command that changes the
 * terminal rather than starting a turn does something the chat cannot undo. Two kinds are declined:
 *
 * - `takes-over-input`: the command replaces Claude Code's input box with a full-pane view. The
 *   agent then cannot accept any further message until someone dismisses the view in the terminal,
 *   which a chat user has no way to do.
 * - `ends-session`: the command shuts the agent's session down. The agent stops responding
 *   entirely, and `mngr message` reports the send as successful while it happens.
 *
 * Which commands behave either way is a fact about Claude Code, not about the chat, so it lives in
 * its own module rather than inline in the composer.
 *
 * The composer applies this unconditionally, with no check of which kind of agent is on the other
 * end. That is safe only because this chat cannot show a non-Claude agent at all: every message it
 * renders comes from parsing Claude Code's own session transcript, and sign-in is handled by
 * Claude-specific auth code. If the chat ever gains a second agent type, this list stops being
 * universally correct -- another agent's slash commands are its own -- and the guard has to become
 * per-agent-type instead.
 *
 * Every `takes-over-input` entry was measured against claude 2.1.220 by sending it to a live agent
 * and confirming both that the input box was gone afterwards and that a following message failed to
 * send. The command's kind in Claude's own registry is NOT a reliable predictor and was not used to
 * decide membership: plenty of commands that render an interactive component (`/model`, `/plugin`,
 * `/theme`, `/rewind`, `/version`) leave the input box alone and send fine.
 *
 * Alias spellings sit alongside the command they resolve to, since a user can type either and
 * Claude treats them identically -- `/cost` and `/stats` are `/usage`, `/settings` is `/config`,
 * `/allowed-tools` is `/permissions`, `/bashes` is `/tasks`, `/quit` is `/exit`. Not duplicates.
 */

export type DeclineReason = "takes-over-input" | "ends-session";

const DECLINED_SLASH_COMMANDS: Readonly<Record<string, DeclineReason>> = {
  "/add-dir": "takes-over-input",
  "/allowed-tools": "takes-over-input",
  "/bashes": "takes-over-input",
  "/config": "takes-over-input",
  "/cost": "takes-over-input",
  "/diff": "takes-over-input",
  "/exit": "ends-session",
  "/extra-usage": "takes-over-input",
  "/goal": "takes-over-input",
  "/help": "takes-over-input",
  "/hooks": "takes-over-input",
  "/ide": "takes-over-input",
  "/mcp": "takes-over-input",
  "/permissions": "takes-over-input",
  "/powerup": "takes-over-input",
  "/privacy-settings": "takes-over-input",
  "/quit": "ends-session",
  "/release-notes": "takes-over-input",
  "/settings": "takes-over-input",
  "/skills": "takes-over-input",
  "/stats": "takes-over-input",
  "/status": "takes-over-input",
  "/tasks": "takes-over-input",
  // Declined on the strength of its argument form: bare `/theme` sends fine, but `/theme dark`
  // takes over the input box. Matching by name covers both.
  "/theme": "takes-over-input",
  "/usage": "takes-over-input",
  "/usage-credits": "takes-over-input",
  "/workflows": "takes-over-input",
};

export interface DeclinedSlashCommand {
  command: string;
  reason: DeclineReason;
}

/** Every declined command, for tests and for anything that wants to show the list. */
export function listDeclinedSlashCommands(): readonly string[] {
  return Object.keys(DECLINED_SLASH_COMMANDS).sort();
}

/**
 * The declined command this message would run, or null if it would not run one.
 *
 * Matched on the command name only, so an argument does not slip the command through: Claude
 * ignores trailing text for these commands and acts regardless. This is deliberately more
 * conservative than the exact-match interception used for `/login` and `/logout`, where an argument
 * genuinely changes what the command does.
 */
export function findDeclinedSlashCommand(text: string): DeclinedSlashCommand | null {
  const firstToken = text.trim().toLowerCase().split(/\s+/, 1)[0] ?? "";
  const reason: DeclineReason | undefined = DECLINED_SLASH_COMMANDS[firstToken];
  return reason === undefined ? null : { command: firstToken, reason };
}
