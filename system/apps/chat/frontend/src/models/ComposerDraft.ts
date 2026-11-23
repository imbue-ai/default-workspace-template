/**
 * A chat's composer draft, as a view outside the composer reaches it.
 *
 * The composer's text is the composer's own (``views/MessageInput.ts``): it keeps a copy in ``localStorage`` as
 * the text changes, so a draft survives a reload, and that copy is what a view outside reads. Such a view changes
 * the draft by writing the copy and saying so here; the mounted composer takes the copy up on its next view pass.
 *
 * A picker in a message (``views/StudioPickers.ts``) edits the draft this way: each of its pills toggles one
 * fragment of it, the pill's label and a comma, and reads as pressed while the draft holds that fragment, so the
 * picker keeps no state of its own.
 */

import m from "mithril";

const MESSAGE_TEXT_KEY_PREFIX = "message-text:";

/** Where ``chatId``'s draft is kept in ``localStorage``. */
export function messageTextKey(chatId: string): string {
  return `${MESSAGE_TEXT_KEY_PREFIX}${chatId}`;
}

/** ``chatId``'s draft as last saved; empty for a chat with none. */
export function readComposerDraft(chatId: string): string {
  return localStorage.getItem(messageTextKey(chatId)) ?? "";
}

/** What a label adds to a draft: the label, then a comma and a space for the next one to follow. */
export function composerFragment(label: string): string {
  return `${label}, `;
}

/** Where ``label``'s fragment stands in ``draft`` as an item of its own (at the draft's start, or after another
 *  item's comma or a line break), or -1: "Drive, " is no item of "Google Drive, ". */
function fragmentIndex(draft: string, label: string): number {
  const fragment = composerFragment(label);
  for (let at = draft.indexOf(fragment); at !== -1; at = draft.indexOf(fragment, at + 1)) {
    if (at === 0 || draft.startsWith(", ", at - 2) || draft[at - 1] === "\n") return at;
  }
  return -1;
}

/** Whether ``chatId``'s draft holds ``label``'s fragment. */
export function isComposerFragmentInDraft(chatId: string, label: string): boolean {
  return fragmentIndex(readComposerDraft(chatId), label) !== -1;
}

/**
 * ``draft`` with ``label``'s fragment taken out when it holds it, else added at its end, so the fragments stand
 * in the order they were added. A draft that ends mid-phrase gets a comma first, so the label reads as the next
 * item rather than as more of the last word.
 */
export function withComposerFragmentToggled(draft: string, label: string): string {
  const fragment = composerFragment(label);
  const at = fragmentIndex(draft, label);
  if (at !== -1) return draft.slice(0, at) + draft.slice(at + fragment.length);
  if (draft === "" || /\s$/.test(draft)) return draft + fragment;
  return `${draft}${draft.endsWith(",") ? " " : ", "}${fragment}`;
}

// The chats whose saved draft a view outside the composer changed, for the mounted composer to take up.
const changedDrafts = new Set<string>();

/** Add ``label``'s fragment to ``chatId``'s draft, or take it out when the draft already holds it. */
export function toggleComposerFragment(chatId: string, label: string): void {
  const next = withComposerFragmentToggled(readComposerDraft(chatId), label);
  if (next === "") localStorage.removeItem(messageTextKey(chatId));
  else localStorage.setItem(messageTextKey(chatId), next);
  changedDrafts.add(chatId);
  m.redraw();
}

/** Whether ``chatId``'s saved draft was changed from outside the composer since this was last asked; the composer
 *  asks on each view pass and reads the saved draft when it was. */
export function takeComposerDraftChange(chatId: string): boolean {
  return changedDrafts.delete(chatId);
}
