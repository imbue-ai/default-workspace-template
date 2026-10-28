/**
 * A turn that failed on the chat's sign-in, once that account has been signed in again.
 *
 * Every chat page re-reads the account list whenever it changes, wherever the sign-in finished,
 * and each account carries when it last signed in. A failed turn older than that is resolved: it
 * stops asking the user to sign in. When the chat app is sending the refused message again itself
 * (it does for an agent the sign-in restarted), the turn says so; otherwise, while it is still the
 * chat's newest turn, it offers to send that message again. Once a copy of a refused message is in
 * the chat, the original and the failure are hidden, so the message shows once.
 *
 * Which messages a failure refused is decided as the chat app decides it
 * (``user_turns_refused_for_sign_in``), so the page and the backend never disagree.
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

/** Whether the chat app is sending a refused message again on its own after the account signed in again. */
export function isResendingAfterSignIn(chatId: string): boolean {
  return getChatById(chatId)?.active_agent.is_resending_after_sign_in === true;
}

/** The user's messages one or more sign-in failures refused, between two of the agent's real replies. */
export interface RefusedTurn {
  /** The failed replies, in transcript order. */
  failures: AssistantMessageEvent[];
  /** The messages they refused, in transcript order. */
  prompts: UserMessageEvent[];
  /** Whether no real reply came after: the turn is the chat's last. */
  isLast: boolean;
  /** Whether the user sent anything else after the last failure. */
  isAnsweredSince: boolean;
}

function isAuthFailure(event: TranscriptEvent): event is AssistantMessageEvent {
  return event.type === "assistant_message" && event.is_auth_error === true;
}

/** Whether `later` was sent after `earlier`: by timestamp when both have one, since claude can write
 *  its sign-in failure into the transcript ahead of the message it refused; by position otherwise. */
function isSentAfter(
  later: TranscriptEvent,
  laterIndex: number,
  earlier: TranscriptEvent,
  earlierIndex: number,
): boolean {
  const laterAt = Date.parse(later.timestamp);
  const earlierAt = Date.parse(earlier.timestamp);
  return Number.isNaN(laterAt) || Number.isNaN(earlierAt) ? laterIndex > earlierIndex : laterAt > earlierAt;
}

/**
 * Every refused turn of the transcript, oldest first. Between two real replies, the user's messages
 * sent no later than the last sign-in failure are the refused ones. A turn the agent had worked on
 * before its sign-in failed has a real reply between the message and the failure, so it is never one.
 */
export function refusedTurns(events: readonly TranscriptEvent[]): RefusedTurn[] {
  const turns: RefusedTurn[] = [];
  let start = 0;
  for (let end = 0; end <= events.length; end++) {
    const event = end < events.length ? events[end] : undefined;
    const isLast = event === undefined;
    if (event !== undefined && (event.type !== "assistant_message" || event.is_auth_error === true)) continue;
    const failures: { event: AssistantMessageEvent; index: number }[] = [];
    for (let index = start; index < end; index++) {
      const candidate = events[index];
      if (isAuthFailure(candidate)) failures.push({ event: candidate, index });
    }
    const lastFailure = failures[failures.length - 1];
    if (lastFailure !== undefined) {
      const prompts: UserMessageEvent[] = [];
      let isAnsweredSince = false;
      for (let index = start; index < end; index++) {
        const candidate = events[index];
        if (candidate.type !== "user_message" || !isTurnUserMessage(candidate)) continue;
        if (isSentAfter(candidate, index, lastFailure.event, lastFailure.index)) isAnsweredSince = true;
        else prompts.push(candidate);
      }
      if (prompts.length > 0) {
        turns.push({ failures: failures.map((failure) => failure.event), prompts, isLast, isAnsweredSince });
      }
    }
    start = end + 1;
  }
  return turns;
}

function wordsOf(prompt: UserMessageEvent): string {
  return classifyUserMessage(prompt).body.trim();
}

/**
 * The refused messages and failures to hide: a refused message once a copy of it was sent later
 * (the chat app sent it again, the user pressed "Send your message again", or typed it again), and
 * the failures once every message they refused has its copy. Nothing the user wrote is hidden
 * while it exists only once.
 */
export function computeAuthErrorHiddenEventIds(events: readonly TranscriptEvent[]): Set<string> {
  const hidden = new Set<string>();
  for (const turn of refusedTurns(events)) {
    const lastFailure = turn.failures[turn.failures.length - 1];
    const lastFailureIndex = events.indexOf(lastFailure);
    const laterWords = new Set<string>();
    events.forEach((event, index) => {
      if (event.type !== "user_message" || !isTurnUserMessage(event) || turn.prompts.includes(event)) return;
      if (isSentAfter(event, index, lastFailure, lastFailureIndex)) laterWords.add(wordsOf(event));
    });
    const copied = turn.prompts.filter((prompt) => wordsOf(prompt) !== "" && laterWords.has(wordsOf(prompt)));
    for (const prompt of copied) hidden.add(prompt.event_id);
    if (copied.length === turn.prompts.length) {
      for (const failure of turn.failures) hidden.add(failure.event_id);
    }
  }
  return hidden;
}

/** The messages `failed` refused, while nothing has happened in the chat since: it is the last
 *  failure of the chat's last turn and the user sent nothing after it. Null otherwise. */
export function messagesToSendAgain(
  events: readonly TranscriptEvent[],
  failed: Pick<AssistantMessageEvent, "event_id">,
): UserMessageEvent[] | null {
  const turn = refusedTurns(events).find(
    (candidate) => candidate.failures[candidate.failures.length - 1].event_id === failed.event_id,
  );
  if (turn === undefined || !turn.isLast || turn.isAnsweredSince) return null;
  const prompts = turn.prompts.filter((prompt) => wordsOf(prompt) !== "");
  return prompts.length > 0 ? prompts : null;
}

export type ResendState = "sending" | "sent" | "failed";

// Keyed by the failed turn's event id. Once the message lands in the transcript the failed turn
// is hidden, whatever its state here.
const resendStates = new Map<string, ResendState>();

export function resendStateOf(failedEventId: string): ResendState | null {
  return resendStates.get(failedEventId) ?? null;
}

/** Send `prompts` again for the turn `failedEventId`, each painted at once like any send. Each goes
 *  as it was first sent, machine context included: the agent never read any of it. */
export async function sendAgain(chatId: string, failedEventId: string, prompts: UserMessageEvent[]): Promise<void> {
  resendStates.set(failedEventId, "sending");
  m.redraw();
  for (const prompt of prompts) {
    const messageId = mintMessageId();
    const outgoingId = addOutgoing(chatId, wordsOf(prompt), messageId);
    m.redraw();
    try {
      const status = await sendMessage(chatId, prompt.content, messageId);
      dropOutgoingNoTurnWillRetire(chatId, outgoingId, status);
    } catch (err) {
      console.error(`Failed to send the message again to chat ${chatId}: ${describeRequestError(err)}`);
      dropOutgoing(chatId, outgoingId);
      resendStates.set(failedEventId, "failed");
      m.redraw();
      return;
    }
  }
  resendStates.set(failedEventId, "sent");
  m.redraw();
}

export function resetResendStatesForTesting(): void {
  resendStates.clear();
}
