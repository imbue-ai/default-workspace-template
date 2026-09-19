/**
 * The chat root's relay for the chat page it nests: the one declared module of this frontend
 * that touches the message primitives (``test_embed_ratchets.py``).
 *
 * A chat page inside the root posts to ``window.parent`` exactly as it would to the shell.
 * Three kinds of those messages are the shell's business and go up unchanged, as the shell's
 * own relay forwards the frames it created (desktop-interface contracts.md section 7): the
 * ``minds:`` messages for the minds chrome, ``shell:focused`` (which the root re-posts as its
 * own, since the shell raises the root's window for it), and ``shell:open`` (a sub-agent view
 * or a sibling chat the page asked for). Everything else a page posts -- its capabilities, its
 * location -- is the root's to know and stops here; the root reports its own.
 *
 * Trust: only a message whose source is one of the root's own inner frames is forwarded, and
 * it is forwarded only to ``window.parent``.
 */

import { SHELL_FOCUSED, SHELL_OPEN } from "@imbue/workspace-ui/src/app_contract";

const MINDS_PREFIX = "minds:";
const FORWARDED_SHELL_TYPES: ReadonlySet<string> = new Set([SHELL_FOCUSED, SHELL_OPEN]);

/** Whether a posted message is one the root passes up to the shell. */
export function isForwardedToShell(data: unknown): boolean {
  if (data === null || typeof data !== "object") return false;
  const type = (data as { type?: unknown }).type;
  if (typeof type !== "string") return false;
  return type.startsWith(MINDS_PREFIX) || FORWARDED_SHELL_TYPES.has(type);
}

/**
 * Start forwarding. ``isInnerWindow`` says whether a message's source is one of the root's
 * inner frames; the root's frame pool answers it.
 */
export function startInnerFrameRelay(isInnerWindow: (source: MessageEventSource | null) => boolean): void {
  window.addEventListener("message", (event: MessageEvent) => {
    if (window.parent === window) return;
    if (!isInnerWindow(event.source)) return;
    if (!isForwardedToShell(event.data)) return;
    window.parent.postMessage(event.data, "*");
  });
}
