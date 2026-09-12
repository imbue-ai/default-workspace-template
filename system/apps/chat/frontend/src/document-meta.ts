/**
 * What the chat app injects into its document's head (``documents.py``): the chat's own
 * identity, the workspace's primary agent, and the terminal app's origin label, so the page
 * reads them off itself rather than guessing them from the URL.
 */

/** The chat the chat document shows. */
export function getChatId(): string {
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

let cachedPrimaryAgentId: string | null = null;

export function getPrimaryAgentId(): string {
  if (cachedPrimaryAgentId !== null) {
    return cachedPrimaryAgentId;
  }
  const metaElement = document.querySelector('meta[name="system-interface-agent-id"]');
  cachedPrimaryAgentId = metaElement?.getAttribute("content") ?? "";
  return cachedPrimaryAgentId;
}
