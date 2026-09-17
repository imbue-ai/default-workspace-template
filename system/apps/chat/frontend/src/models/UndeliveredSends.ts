/**
 * Sends a finished switch could not hand to the agent, taken back into the composer.
 *
 * A message sent while a chat is converging is answered 202 and held for the agent the chat is
 * moving to. If that agent then refuses it -- the account it moved to is out of usage, the
 * commonest case -- the app is holding the only copy of what the user typed. The backend parks
 * it on the chat record and puts it on every snapshot until the composer says it has it.
 *
 * Prepend first, take second: an ack that never lands leaves the send on the record, so the next
 * snapshot offers it again, and the absorbed ids are what stop that from pasting the message
 * into the composer twice.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { postJson } from "@imbue/workspace-ui/src/models/http";
import { addChatsUpdatedListener } from "./Chats";
import type { ChatSnapshot } from "./Chats";
import { prependToComposer } from "../views/MessageInput";

/** Puts each chat's undelivered sends back in its composer, at most once per send.
 *
 * A class rather than module state so each test drives its own, and so the absorbed ids cannot
 * leak between them. The app runs one, built below.
 */
export class UndeliveredSendAbsorber {
  private readonly absorbedMessageIds = new Set<string>();

  constructor(
    private readonly prepend: (chatId: string, block: string) => void = prependToComposer,
    private readonly take: (chatId: string, messageId: string) => Promise<void> = postTakeUndeliveredSend,
  ) {}

  absorb(chats: readonly ChatSnapshot[]): void {
    for (const chat of chats) {
      for (const held of chat.undelivered_sends ?? []) {
        if (this.absorbedMessageIds.has(held.message_id)) continue;
        this.absorbedMessageIds.add(held.message_id);
        this.prepend(chat.chat_id, held.text);
        void this.take(chat.chat_id, held.message_id);
      }
    }
    m.redraw();
  }
}

async function postTakeUndeliveredSend(chatId: string, messageId: string): Promise<void> {
  try {
    await postJson(apiUrl(`/api/chats/${encodeURIComponent(chatId)}/undelivered/take`), { message_id: messageId });
  } catch {
    // Left on the record deliberately: it rides the next snapshot, and the absorbed ids keep
    // that from reaching the composer a second time. Losing the text is the one outcome worth
    // avoiding here.
  }
}

export function connectUndeliveredSends(): void {
  const absorber = new UndeliveredSendAbsorber();
  addChatsUpdatedListener((chats) => absorber.absorb(chats));
}
