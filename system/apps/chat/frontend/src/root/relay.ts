/**
 * The chat root's relay for the chat page it nests: the one declared module of this frontend
 * that touches the message primitives (``test_embed_ratchets.py``).
 *
 * A chat page inside the root posts to ``window.parent`` exactly as it would to the shell.
 * Three kinds of those messages are the shell's business and go up unchanged, as the shell's
 * own relay forwards the frames it created (desktop-interface contracts.md section 7): the
 * ``minds:`` messages for the minds chrome, ``shell:focused`` (which the root re-posts as its
 * own, since the shell raises the root's window for it), and ``shell:open`` of a sub-agent view.
 * One ``shell:open`` is the root's own business: a page asking for a sibling chat names the
 * root's path for it (``/?chat=<id>``), and the root that already frames a chat list selects
 * that chat in place, exactly as its own New chat button does, rather than asking the shell for
 * a second root window. Everything else a page posts -- its capabilities, its location -- is the
 * root's to know and stops here; the root reports its own.
 *
 * Trust: only a message whose source is one of the root's own inner frames is acted on, and a
 * forwarded one is forwarded only to ``window.parent``.
 */

import { SHELL_FOCUSED, SHELL_OPEN } from "@imbue/workspace-ui/src/app_contract";
import { selectionFromSearch } from "./selection";

const MINDS_PREFIX = "minds:";
const FORWARDED_SHELL_TYPES: ReadonlySet<string> = new Set([SHELL_FOCUSED, SHELL_OPEN]);

/** Whether a posted message is one the root passes up to the shell. */
export function isForwardedToShell(data: unknown): boolean {
  if (data === null || typeof data !== "object") return false;
  const type = (data as { type?: unknown }).type;
  if (typeof type !== "string") return false;
  return type.startsWith(MINDS_PREFIX) || FORWARDED_SHELL_TYPES.has(type);
}

/** What a ``shell:open`` from an inner page comes to at the root. */
export type RootOpenDecision =
  | { readonly kind: "select"; readonly chatId: string | null }
  | { readonly kind: "forward" }
  | { readonly kind: "not-an-open" };

/**
 * A ``shell:open`` whose path is a root path (``/`` or ``/?chat=<id>``) is the root's to answer
 * by selecting the chat in place; any other path (a sub-agent view) is the shell's. The address
 * form the tabbed shell took is nobody's now and is forwarded for the shell to refuse.
 */
export function rootOpenDecision(data: unknown): RootOpenDecision {
  if (data === null || typeof data !== "object") return { kind: "not-an-open" };
  const message = data as { type?: unknown; path?: unknown };
  if (message.type !== SHELL_OPEN) return { kind: "not-an-open" };
  if (typeof message.path !== "string") return { kind: "forward" };
  const target = new URL(message.path, "http://root.invalid");
  if (target.pathname !== "/") return { kind: "forward" };
  return { kind: "select", chatId: selectionFromSearch(target.search) };
}

/**
 * Start forwarding. ``isInnerWindow`` says whether a message's source is one of the root's
 * inner frames; the root's frame pool answers it. ``selectChat`` is the root's own selection,
 * for a sibling chat an inner page asks for.
 */
export function startInnerFrameRelay(
  isInnerWindow: (source: MessageEventSource | null) => boolean,
  selectChat: (chatId: string | null) => void,
): void {
  window.addEventListener("message", (event: MessageEvent) => {
    if (!isInnerWindow(event.source)) return;
    const decision = rootOpenDecision(event.data);
    if (decision.kind === "select") {
      selectChat(decision.chatId);
      return;
    }
    if (window.parent === window) return;
    if (!isForwardedToShell(event.data)) return;
    window.parent.postMessage(event.data, "*");
  });
}
