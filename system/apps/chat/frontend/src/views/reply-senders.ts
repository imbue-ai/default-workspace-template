/**
 * The composers a reply button in a message can send through.
 *
 * An agent (or a seeded welcome turn) offers one-click replies as markdown links to ``#reply=<text>``, which
 * markdown.ts draws as buttons. A click sends ``<text>`` as the user's next message exactly as the composer's own
 * send would, so it goes through the composer: each mounted composer registers the sender for the chat it shows,
 * and a button with no composer to send through (a read-only view) is disabled.
 *
 * Kept free of the models on purpose: markdown.ts reads it, and must not pull the chat's state in with it.
 */

import m from "mithril";

/** The href of a reply link: ``[Yes](#reply=yes)`` offers "yes" as the user's next message. A fragment, so a chat
 *  app that predates reply buttons unwraps it to its text (markdown.ts, ``rewritePathLinks``) instead of opening
 *  anything. */
export const REPLY_HREF_PREFIX = "#reply=";

/** Sends ``text`` as the chat's next message, as the composer's send would; false when the composer cannot send
 *  now (it is disabled, or a switch request is out). */
export type ReplySender = (text: string) => boolean;

interface Registration {
  /** The composer that registered it, so a composer leaving a chat drops only its own registration. */
  readonly owner: object;
  readonly send: ReplySender;
}

const sendersByChat = new Map<string, Registration>();

/**
 * Make ``send`` the way a reply button in ``chatId`` sends, for as long as ``owner`` (a mounted composer) shows that
 * chat. Called on each of the composer's renders, so it always holds that render's send; the page redraws only
 * when the chat gains a sender, since only that changes whether its buttons are enabled.
 */
export function registerReplySender(chatId: string, owner: object, send: ReplySender): void {
  const previous = sendersByChat.get(chatId);
  sendersByChat.set(chatId, { owner, send });
  if (previous === undefined) m.redraw();
}

/** Drop ``owner``'s sender for ``chatId`` (its composer left the chat, or cannot send now); another composer's is
 *  kept. */
export function unregisterReplySender(chatId: string, owner: object): void {
  if (sendersByChat.get(chatId)?.owner !== owner) return;
  sendersByChat.delete(chatId);
  m.redraw();
}

/** Whether a reply button in ``chatId`` has a composer to send through. */
export function canSendReply(chatId: string): boolean {
  return sendersByChat.has(chatId);
}

/** Send ``text`` as ``chatId``'s next message through its composer; false when there is none, or it cannot send now. */
export function sendReplyFromMessage(chatId: string, text: string): boolean {
  const registration = sendersByChat.get(chatId);
  return registration !== undefined && registration.send(text);
}
