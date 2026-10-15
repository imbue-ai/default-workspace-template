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
  /** Whether the root draws its phone layout, which the page's composer follows (``compactLayout.ts``). */
  setCompact(isCompact: boolean): void;
}

declare global {
  interface Window {
    chatPageEmbed?: ChatPageEmbedApi;
  }
}
