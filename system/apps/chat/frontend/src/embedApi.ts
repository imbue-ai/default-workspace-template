import type { EventsResponse } from "./models/Response";

/**
 * What a chat page exposes on its window for the chat root that frames it.
 *
 * The root (``root/``) and a chat page share an origin, so the root drives the page directly
 * rather than by messaging (desktop-interface plan section 9.1): it says when the page is shown
 * and hidden by calling these. The shell's handshake alone goes down as a message (the root's
 * relay hands it on), since the page's app contract reads it. The page's replies --
 * ``shell:focused``, ``shell:open``, and the ``minds:`` messages -- travel up as messages, which
 * the root's relay forwards to the shell.
 */

export interface ChatPageEmbedApi {
  /** The root is showing this page. */
  shown(): void;
  /** The root has hidden this page (another chat is selected, or the root itself is hidden). */
  hidden(): void;
  /** Put ``text`` in this page's composer above whatever is there, unsent (the root's ``draft`` param). */
  prependDraft(text: string): void;
  /** Show ``hint`` as the composer's placeholder until the next send, and focus it (the multiplayer spec, section
   *  9.2: "How do you want to change <app>?"). */
  hintComposer(hint: string): void;
  /** Whether the root draws its phone layout, which the page's composer follows (``compactLayout.ts``). */
  setCompact(isCompact: boolean): void;
  /** Take the keyboard, as a page does when it first loads: its composer, else the page itself. The root
   *  calls it when it switches to a page it already held, so the chat the user picked counts as focused. */
  focus(): void;
}

/** What the chat root exposes on its window for the chat pages it frames, which read it as they load. */
export interface ChatRootEmbedApi {
  /** The read of the chat's newest events the root started as it created the page's frame, handed over once (later
   *  calls answer null), so the page's first load does not wait for its own request (``root/transcriptPrefetch.ts``). */
  takeTranscriptSnapshot(chatId: string): Promise<EventsResponse> | null;
}

declare global {
  interface Window {
    chatPageEmbed?: ChatPageEmbedApi;
    chatRootEmbed?: ChatRootEmbedApi;
  }
}

/** The transcript read the chat root framing this page started for ``chatId``, or null: not framed by the root (a
 *  direct visit, or a frame of the shell, whose window this origin cannot reach), or nothing started or left. */
export function takeRootTranscriptSnapshot(chatId: string): Promise<EventsResponse> | null {
  if (typeof window === "undefined" || window.parent === window) return null;
  try {
    return window.parent.chatRootEmbed?.takeTranscriptSnapshot(chatId) ?? null;
  } catch {
    return null;
  }
}
