/**
 * A turn that failed on the chat's sign-in, once that account has been signed in again.
 *
 * Every chat page re-reads the account list whenever it changes, wherever the sign-in finished,
 * and each account carries when it last signed in. A failed turn older than that is
 * resolved: it stops asking the user to sign in, and when it is still the chat's newest turn,
 * it offers to send the message it failed on again. It is offered rather than sent on its own,
 * because the user may have moved on or typed it again.
 */

import m from "mithril";
import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";
import { getChatById } from "../models/Chats";
import { addOutgoing, dropOutgoing, dropOutgoingNoTurnWillRetire } from "../models/OutgoingMessages";
import { accountForAgent } from "../models/Providers";
import { mintMessageId, sendMessage } from "../models/Response";
import type { AssistantMessageEvent, TranscriptEvent, UserMessageEvent } from "../models/Response";
import { classifyUserMessage, isTurnUserMessage } from "./message-classification";

/** Whether the chat's account finished a sign-in after `event` failed on it. */
export function isSignedInSince(event: Pick<AssistantMessageEvent, "timestamp">, chatId: string): boolean {
  const account = accountForAgent(getChatById(chatId)?.active_agent.account_id ?? undefined);
  if (account === null || account.signed_in_at === null) return false;
  return Date.parse(account.signed_in_at) > Date.parse(event.timestamp);
}

/** The message `failed` was the reply to, while nothing has happened in the chat since: `failed`
 *  is its newest assistant turn and no prompt of the user's came after it. Null otherwise, and
 *  for a prompt with none of the user's own words to send. */
export function messageToSendAgain(
  events: readonly TranscriptEvent[],
  failed: Pick<AssistantMessageEvent, "event_id">,
): UserMessageEvent | null {
  let prompt: UserMessageEvent | null = null;
  let isPastFailed = false;
  for (const event of events) {
    if (event.event_id === failed.event_id) {
      isPastFailed = true;
      continue;
    }
    if (event.type === "assistant_message" && isPastFailed) return null;
    if (event.type !== "user_message" || !isTurnUserMessage(event)) continue;
    if (isPastFailed) return null;
    prompt = event;
  }
  if (!isPastFailed || prompt === null || classifyUserMessage(prompt).body.trim() === "") return null;
  return prompt;
}

export type ResendState = "sending" | "sent" | "failed";

// Keyed by the failed turn's event id. Once the message lands in the transcript the failed turn
// is no longer the newest and offers nothing, whatever its state here.
const resendStates = new Map<string, ResendState>();

export function resendStateOf(failedEventId: string): ResendState | null {
  return resendStates.get(failedEventId) ?? null;
}

/** Send `prompt` again for the turn `failedEventId`, painted at once like any send. Only the
 *  user's own words go again, not machine context the turn carried with them. */
export async function sendAgain(chatId: string, failedEventId: string, prompt: UserMessageEvent): Promise<void> {
  const content = classifyUserMessage(prompt).body;
  const messageId = mintMessageId();
  const outgoingId = addOutgoing(chatId, content, messageId);
  resendStates.set(failedEventId, "sending");
  m.redraw();
  try {
    const status = await sendMessage(chatId, content, messageId);
    dropOutgoingNoTurnWillRetire(chatId, outgoingId, status);
    resendStates.set(failedEventId, "sent");
  } catch (err) {
    console.error(`Failed to send the message again to chat ${chatId}: ${describeRequestError(err)}`);
    dropOutgoing(chatId, outgoingId);
    resendStates.set(failedEventId, "failed");
  }
  m.redraw();
}

export function resetResendStatesForTesting(): void {
  resendStates.clear();
}
