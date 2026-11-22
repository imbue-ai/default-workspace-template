/**
 * The chat root's relay for the chat page it nests: the one declared module of this frontend
 * that touches the message primitives (``test_embed_ratchets.py``).
 *
 * A chat page inside the root posts to ``window.parent`` exactly as it would to the shell.
 * Some of those messages are the shell's business and go up unchanged, as the shell's own relay
 * forwards the frames it created (desktop-interface contracts.md section 7): the ``minds:``
 * messages for the minds chrome, ``shell:focused`` (which the root re-posts as its own, since the
 * shell raises the root's window for it), ``shell:input`` (typing in a nested page is this client's
 * activity, which the shell's heartbeat reports), ``shell:open`` of a sub-agent view, ``shell:draft-text``
 * from a sub-agent view (which has no composer of its own to draft into), ``shell:show-coach-card`` (a
 * card under a control of the minds chrome, which only the shell can draw), ``shell:message`` (for
 * whichever app registered its type), and ``shell:open-link`` (a clicked link or script popup the
 * app contract hands on -- a local URL, another app's address, a ``file:`` URL, an external web,
 * ``mailto:`` or ``tel:`` link -- which the shell opens where it belongs).
 * One ``shell:open`` is the root's own business: a page asking for a sibling chat names the
 * root's path for it (``/?chat=<id>``), and the root that already frames a chat list selects
 * that chat in place, exactly as its own New chat button does, rather than asking the shell for
 * a second root window. A page's ``shell:agent-working`` (whether a reply is under way in its chat)
 * stops here too, and the root says its own: the shell hears every nested page as the root, one
 * frame, so the root keeps each page's last word (``agentWorkingReports.ts``) and tells the shell
 * that an agent is working while any page it holds says so, when that changes and again when the
 * shell greets the root while one is. A page whose frame is destroyed or loads again takes its
 * word with it. Everything else a page posts -- its capabilities, its location -- is the
 * root's to know and stops here; the root reports its own. A root opened on its own has no shell
 * to forward to, and drops them.
 *
 * Two kinds go the other way. The shell's handshake to the root is handed to each inner page
 * (``handHandshakeDown``), so a page's app contract knows a shell stands above it; a page under a
 * root opened on its own gets none, and leaves its links to the browser. And the minds chrome
 * answers some of an inner page's asks, and its answer reaches the root, which the shell frames,
 * not the page that asked: those answers (``minds:provider-sign-in-ack``, and
 * ``minds:permission-resolutions``, the verdict on a permission request a page asked the chrome
 * to open, which the page's card flips on and whose activity strip covers the wait for the agent
 * to be told) are passed down to the root's inner frames, as is the shell's
 * ``shell:keybindings-changed`` (each inner page re-fetches its resolved chords). Everything else
 * the chrome sends is the root's own to handle.
 *
 * Trust: a message going up counts only when its source is one of the root's own inner frames,
 * and is forwarded only to ``window.parent``; a message going down counts only when its source is
 * ``window.parent``, and is forwarded only to the root's inner frames.
 */

import {
  SHELL_AGENT_WORKING,
  SHELL_DRAFT_TEXT,
  SHELL_FOCUSED,
  SHELL_HANDSHAKE,
  SHELL_INPUT,
  SHELL_KEYBINDINGS_CHANGED,
  SHELL_MESSAGE,
  SHELL_OPEN,
  SHELL_OPEN_LINK,
  SHELL_SHOW_COACH_CARD,
} from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { PERMISSION_RESOLUTIONS, PROVIDER_SIGN_IN_ACK } from "@imbue/workspace-ui/src/embed";
import { makeAgentWorkingReports } from "./agentWorkingReports";
import { selectionFromSearch } from "./selection";

const MINDS_PREFIX = "minds:";
const FORWARDED_SHELL_TYPES: ReadonlySet<string> = new Set([
  SHELL_FOCUSED,
  SHELL_INPUT,
  SHELL_OPEN,
  SHELL_DRAFT_TEXT,
  SHELL_SHOW_COACH_CARD,
  SHELL_MESSAGE,
  SHELL_OPEN_LINK,
]);
const FORWARDED_TO_INNER_TYPES: ReadonlySet<string> = new Set([
  PROVIDER_SIGN_IN_ACK,
  PERMISSION_RESOLUTIONS,
  SHELL_KEYBINDINGS_CHANGED,
]);

/** Whether a posted message is one the root passes up to the shell. */
export function isForwardedToShell(data: unknown): boolean {
  if (data === null || typeof data !== "object") return false;
  const type = (data as { type?: unknown }).type;
  if (typeof type !== "string") return false;
  return type.startsWith(MINDS_PREFIX) || FORWARDED_SHELL_TYPES.has(type);
}

/** Whether a message from the parent is one of the chrome's answers to an inner page, which the root passes down. */
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

/** What the root does with what its inner pages said of an agent working, beyond hearing them. */
export interface InnerFrameRelay {
  /** The page in ``inner`` is gone, or is a fresh page now (its frame loaded): what it said goes with it. */
  forgetAgentWorking(inner: MessageEventSource): void;
  /** The shell greeted the root, having forgotten what the root said: an agent still working is told again. */
  repeatAgentWorking(): void;
}

/** Whether an inner page's ``shell:agent-working`` says an agent is working; null for any other message, and for
 *  one that carries no boolean. */
function agentWorkingReportOf(data: unknown): boolean | null {
  if (data === null || typeof data !== "object") return null;
  const message = data as { type?: unknown; isWorking?: unknown };
  return message.type === SHELL_AGENT_WORKING && typeof message.isWorking === "boolean" ? message.isWorking : null;
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
): InnerFrameRelay {
  const tellShellAgentWorking = (isWorking: boolean): void => {
    if (window.parent !== window) window.parent.postMessage({ type: SHELL_AGENT_WORKING, isWorking }, "*");
  };
  const agentWorking = makeAgentWorkingReports<MessageEventSource>(tellShellAgentWorking);
  window.addEventListener("message", (event: MessageEvent) => {
    if (window.parent !== window && event.source === window.parent) {
      if (!isForwardedToInnerFrames(event.data)) return;
      for (const inner of innerWindows()) inner.postMessage(event.data, "*");
      return;
    }
    if (event.source === null || !isInnerWindow(event.source)) return;
    const isAgentWorking = agentWorkingReportOf(event.data);
    if (isAgentWorking !== null) {
      agentWorking.note(event.source, isAgentWorking);
      return;
    }
    const decision = rootOpenDecision(event.data);
    if (decision.kind === "select") {
      selectChat(decision.chatId);
      return;
    }
    if (window.parent === window || !isForwardedToShell(event.data)) return;
    window.parent.postMessage(event.data, "*");
  });
  return {
    forgetAgentWorking: (inner) => agentWorking.forget(inner),
    repeatAgentWorking: () => {
      if (agentWorking.isAnyWorking()) tellShellAgentWorking(true);
    },
  };
}

/** Hand the shell's handshake to the root to ``inner``, one of its inner pages, as the shell hands it to the root. */
export function handHandshakeDown(inner: Window, handshake: ShellHandshake): void {
  inner.postMessage({ type: SHELL_HANDSHAKE, ...handshake }, "*");
}
