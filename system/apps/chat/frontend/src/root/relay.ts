/**
 * The chat root's relay for the chat page it nests: the one declared module of this frontend
 * that touches the message primitives (``test_embed_ratchets.py``).
 *
 * A chat page inside the root posts to ``window.parent`` exactly as it would to the shell.
 * Some of those messages are the shell's business and go up unchanged, as the shell's own relay
 * forwards the frames it created (desktop-interface contracts.md section 7): the ``minds:``
 * messages for the minds chrome, ``shell:focused`` (which the root re-posts as its own, since the
 * shell raises the root's window for it), ``shell:open`` of a sub-agent view, ``shell:draft-text``
 * from a sub-agent view (which has no composer of its own to draft into), ``shell:message`` (for
 * whichever app registered its type), and ``shell:open-link`` (a clicked link the app contract
 * hands on -- a local URL, another app's address, a ``file:`` URL -- which the shell opens where
 * it belongs).
 * One ``shell:open`` is the root's own business: a page asking for a sibling chat names the
 * root's path for it (``/?chat=<id>``), and the root that already frames a chat list selects
 * that chat in place, exactly as its own New chat button does, rather than asking the shell for
 * a second root window. Everything else a page posts -- its capabilities, its location -- is the
 * root's to know and stops here; the root reports its own. A root opened on its own has no shell
 * to forward to, so it opens a page's web, ``mailto:`` or ``tel:`` ``shell:open-link`` itself, as
 * a page with no shell around it does; a ``file:`` link has nowhere to open there.
 *
 * One kind goes the other way. The minds chrome answers some of an inner page's asks, and its
 * answer reaches the root, which the shell frames, not the page that asked: those answers
 * (``minds:provider-sign-in-ack``) are passed down to the root's inner frames. Everything else
 * the chrome sends is the root's own to handle.
 *
 * Trust: a message going up counts only when its source is one of the root's own inner frames,
 * and is forwarded only to ``window.parent``; a message going down counts only when its source is
 * ``window.parent``, and is forwarded only to the root's inner frames.
 */

import {
  SHELL_DRAFT_TEXT,
  SHELL_FOCUSED,
  SHELL_MESSAGE,
  SHELL_OPEN,
  SHELL_OPEN_LINK,
} from "@imbue/workspace-ui/src/app_contract";
import { PROVIDER_SIGN_IN_ACK } from "@imbue/workspace-ui/src/embed";
import { selectionFromSearch } from "./selection";

const MINDS_PREFIX = "minds:";
const FORWARDED_SHELL_TYPES: ReadonlySet<string> = new Set([
  SHELL_FOCUSED,
  SHELL_OPEN,
  SHELL_DRAFT_TEXT,
  SHELL_MESSAGE,
  SHELL_OPEN_LINK,
]);
const FORWARDED_TO_INNER_TYPES: ReadonlySet<string> = new Set([PROVIDER_SIGN_IN_ACK]);

/** Whether a posted message is one the root passes up to the shell. */
export function isForwardedToShell(data: unknown): boolean {
  if (data === null || typeof data !== "object") return false;
  const type = (data as { type?: unknown }).type;
  if (typeof type !== "string") return false;
  return type.startsWith(MINDS_PREFIX) || FORWARDED_SHELL_TYPES.has(type);
}

/** Whether a message from the parent is the chrome's answer to an inner page, which the root passes down. */
export function isForwardedToInnerFrames(data: unknown): boolean {
  if (data === null || typeof data !== "object") return false;
  const type = (data as { type?: unknown }).type;
  return typeof type === "string" && FORWARDED_TO_INNER_TYPES.has(type);
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
 * inner frames, and ``innerWindows`` lists them; the root's frame pool answers both.
 * ``selectChat`` is the root's own selection, for a sibling chat an inner page asks for.
 */
export function startInnerFrameRelay(
  isInnerWindow: (source: MessageEventSource | null) => boolean,
  innerWindows: () => readonly Window[],
  selectChat: (chatId: string | null) => void,
): void {
  window.addEventListener("message", (event: MessageEvent) => {
    if (window.parent !== window && event.source === window.parent) {
      if (!isForwardedToInnerFrames(event.data)) return;
      for (const inner of innerWindows()) inner.postMessage(event.data, "*");
      return;
    }
    if (!isInnerWindow(event.source)) return;
    const decision = rootOpenDecision(event.data);
    if (decision.kind === "select") {
      selectChat(decision.chatId);
      return;
    }
    if (window.parent === window) {
      const url = webLinkToOpen(event.data);
      if (url !== null) window.open(url, "_blank", "noopener");
      return;
    }
    if (!isForwardedToShell(event.data)) return;
    window.parent.postMessage(event.data, "*");
  });
}

/** The http(s), ``mailto:`` or ``tel:`` URL of a ``shell:open-link``, or null for any other message or URL. */
function webLinkToOpen(data: unknown): string | null {
  if (data === null || typeof data !== "object") return null;
  const message = data as { type?: unknown; url?: unknown };
  if (message.type !== SHELL_OPEN_LINK || typeof message.url !== "string") return null;
  return /^(?:https?:\/\/|mailto:|tel:)/i.test(message.url) ? message.url : null;
}
