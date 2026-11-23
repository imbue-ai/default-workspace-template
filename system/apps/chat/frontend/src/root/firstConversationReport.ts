/**
 * What the chat root tells the shell of the workspace's first conversation (``shell:first-conversation-step``).
 *
 * A new workspace's first chat opens on a short conversation that follows a tree (the backend's
 * ``first_run_tree.py``), and the shell holds its show-around tour until that is over, so the two never run on top of
 * each other. Every chat list the chat app pushes says whether the conversation is still following its tree; the
 * root, which is always loaded in the shell's chat window, passes that on: once it has a chat list, each time the
 * answer changes, and again when a shell greets it, since a shell forgets what a page said when the page's frame
 * loads.
 */

export interface FirstConversationReport {
  /** What the last chat list said; null while none has arrived, which is told to nobody. */
  note(isPending: boolean | null): void;
  /** A shell greeted the root, having heard nothing from this page: the next answer is told even when unchanged. */
  forgetTold(): void;
}

/** A report that calls ``tell`` with each answer the shell has not been told. */
export function makeFirstConversationReport(tell: (isPending: boolean) => void): FirstConversationReport {
  let told: boolean | null = null;
  return {
    note: (isPending) => {
      if (isPending === null || isPending === told) return;
      told = isPending;
      tell(isPending);
    },
    forgetTold: () => {
      told = null;
    },
  };
}
