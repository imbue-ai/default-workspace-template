/**
 * What the chat app injects into its document's head (``documents.py``): the chat's own
 * identity (and, on a subagent page, the agent and session it shows) and the terminal app's
 * origin label, so the page reads them off itself rather than guessing them from the URL.
 */

/** The chat the chat document shows. */
export function getChatId(): string {
  return document.querySelector('meta[name="system-interface-chat-id"]')?.getAttribute("content") ?? "";
}

/** What the chat is: ``room`` for a room's page (which draws no model bar and tags the agent by mention), else ``agent``. */
export function getChatKind(): "agent" | "room" {
  // Read from a model as well as from views, so it tolerates a test's document-less environment.
  if (typeof document === "undefined" || typeof document.querySelector !== "function") return "agent";
  const kind = document.querySelector('meta[name="system-interface-chat-kind"]')?.getAttribute("content");
  return kind === "room" ? "room" : "agent";
}

/** The agent whose session a subagent page shows; "" for a chat's own page. */
export function getChatAgentId(): string {
  return document.querySelector('meta[name="system-interface-chat-agent-id"]')?.getAttribute("content") ?? "";
}

/** The terminal app's origin label, which the chat app reads from the registry into the page. */
export function getTerminalOriginLabel(): string {
  return document.querySelector('meta[name="system-interface-terminal-label"]')?.getAttribute("content") ?? "";
}

/** The subagent session the chat document shows; "" for a chat's own page. */
export function getChatSessionId(): string {
  return document.querySelector('meta[name="system-interface-chat-session-id"]')?.getAttribute("content") ?? "";
}
