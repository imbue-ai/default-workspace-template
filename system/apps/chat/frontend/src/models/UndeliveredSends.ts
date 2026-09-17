/**
 * Sends a finished switch could not hand to the agent, taken back into the composer.
 *
 * A message sent while a chat is converging is answered 202 and held for the agent the chat is
 * moving to. If that agent then refuses it -- the account it moved to being out of usage too,
 * the commonest case -- the app is holding the only copy of what the user typed. The backend
 * parks it on the chat record and puts it on every snapshot until a page says it has it.
 *
 * Prepend first, take second, and take again on every later sighting: the take is idempotent by
 * message id, so re-issuing it costs one request and is what stops a send whose first take was
 * lost from sitting on the record forever.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { postJson } from "@imbue/workspace-ui/src/models/http";
import { asSendFailureKind } from "@imbue/workspace-ui/src/models/request-error";
import { addChatsUpdatedListener } from "./Chats";
import type { ChatSnapshot } from "./Chats";
import { prependToComposer, raiseFailureNotice } from "../views/MessageInput";
import { getChatSessionId } from "../document-meta";

/** Puts a chat's undelivered sends back in its composer, each of them once.
 *
 * A class rather than module state so each test drives its own and the absorbed ids cannot leak
 * between them. The app builds one, for its own page's chat only.
 */
export class UndeliveredSendAbsorber {
  private readonly pastedMessageIds = new Set<string>();

  constructor(
    private readonly ownChatId: string,
    private readonly prepend: (chatId: string, block: string) => void = prependToComposer,
    private readonly take: (chatId: string, messageId: string) => Promise<void> = postTakeUndeliveredSend,
    private readonly raise: (chatId: string, detail: string, kind: string) => void = raiseUndeliveredNotice,
  ) {}

  absorb(chats: readonly ChatSnapshot[]): void {
    // This page's chat only. Every open frame of every chat gets the same broadcast, and the
    // shell keeps hidden frames alive, so absorbing the whole list would paste one message into
    // one composer once per open pane -- deterministically, not as a race.
    const chat = chats.find((candidate) => candidate.chat_id === this.ownChatId);
    if (chat === undefined) return;
    const fresh = chat.undelivered_sends.filter((held) => !this.pastedMessageIds.has(held.message_id));
    if (fresh.length > 0) {
      // One paste for all of them, in the order they were sent. A paste per send would invert
      // them: each one goes in ABOVE what the composer already holds, so the last would end up
      // on top. One notice too -- the composer keeps only the latest anyway, and sends refused
      // together were refused by one agent for one reason.
      for (const held of fresh) this.pastedMessageIds.add(held.message_id);
      this.prepend(chat.chat_id, fresh.map((held) => held.text).join("\n\n"));
      this.raise(chat.chat_id, fresh[0].detail, fresh[0].kind);
    }
    for (const held of chat.undelivered_sends) {
      // The take is re-issued even for one already pasted: a take that never landed leaves the
      // send on the record, and this is the only thing that clears it.
      void this.take(chat.chat_id, held.message_id);
    }
  }
}

async function postTakeUndeliveredSend(chatId: string, messageId: string): Promise<void> {
  try {
    await postJson(apiUrl(`/api/chats/${encodeURIComponent(chatId)}/undelivered/take`), { message_id: messageId });
  } catch {
    // Left on the record deliberately: it rides the next snapshot, where the take is issued
    // again and the pasted ids keep the text from reaching the composer twice.
  }
}

function raiseUndeliveredNotice(chatId: string, detail: string, kind: string): void {
  // No retry offered: the text is back in the composer, and on the commonest cause of this --
  // the account the chat moved to being out of usage -- sending it again earns the same refusal.
  raiseFailureNotice(chatId, {
    title: "Your message wasn't sent",
    detail: `${detail}\n\nIt's back in the composer.`,
    kind: asSendFailureKind(kind),
  });
}

export function connectUndeliveredSends(chatId: string): void {
  // The chat's own page only, the rule its presence report follows: a subagent view is a second
  // page of the same chat in the same client, and it would paste the same message a second time.
  if (getChatSessionId() !== "") return;
  const absorber = new UndeliveredSendAbsorber(chatId);
  addChatsUpdatedListener((chats) => absorber.absorb(chats));
}
