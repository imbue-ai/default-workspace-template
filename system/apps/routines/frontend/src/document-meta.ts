/**
 * What the Routines app injects into its document's head (``documents.py``): the chat app's origin
 * label, so the pages link a run's chat on the chat app's real origin rather than guessing it.
 */

/** The chat app's origin label, read from the registry into the page; "" before it registers. */
export function getChatOriginLabel(): string {
  return document.querySelector('meta[name="routines-chat-label"]')?.getAttribute("content") ?? "";
}
