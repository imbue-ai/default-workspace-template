/**
 * What a chat page exposes on its window for the chat root that frames it.
 *
 * The root (``root/``) and a chat page share an origin, so the root drives the page directly
 * rather than by messaging (desktop-interface plan section 9.1): it hands over the shell's
 * handshake and says when the page is shown and hidden by calling these. The page's replies
 * -- ``shell:focused``, ``shell:open``, and the ``minds:`` messages -- still travel up as
 * messages, which the root's relay forwards to the shell.
 */

import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";

export interface ChatPageEmbedApi {
  /** The handshake the shell gave the root, passed down so the page adopts the same client. */
  handshake(handshake: ShellHandshake): void;
  /** The root is showing this page. */
  shown(): void;
  /** The root has hidden this page (another chat is selected, or the root itself is hidden). */
  hidden(): void;
}

declare global {
  interface Window {
    chatPageEmbed?: ChatPageEmbedApi;
  }
}

/** Whether ``window.parent`` is a same-origin document (the chat root) rather than the shell.
 *
 * Reading a cross-origin parent's location throws, which is the whole test: the shell frames
 * chat pages from its own origin, and only the root frames them from the chat's. */
export function isFramedBySameOrigin(): boolean {
  if (window.parent === window) return false;
  try {
    return window.parent.location.origin === window.location.origin;
  } catch {
    return false;
  }
}
