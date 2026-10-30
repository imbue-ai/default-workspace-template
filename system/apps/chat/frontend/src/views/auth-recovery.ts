/**
 * A turn that failed on the chat's sign-in, once that account has been signed in again.
 *
 * Every chat page re-reads the account list whenever it changes, wherever the sign-in finished,
 * and each account carries when it last signed in. A failed turn older than that is resolved: it
 * stops asking the user to sign in. When the chat app is sending the refused message again itself
 * (it does for an agent the sign-in restarted), the turn says so; otherwise, while it is still the
 * chat's newest turn, it offers to send that message again, and after a resend that stopped
 * partway, the messages it did not get to. Once a refused message is sent again right after the
 * failure, before the user writes anything else, the original and the failure are hidden, so the
 * message shows once.
 *
 * Which messages a failure refused follows the chat app's rule (``user_turns_refused_for_sign_in``):
 * the user's messages after the agent's last real reply, sent no later than the last failure, by
 * timestamp. The page counts only the user's own messages; the chat app also sends a successor's
 * handoff prompt again, which the page never offers.
 */

import m from "mithril";
import {
  describeRequestError,
  describeRequestErrorKind,
  isUnansweredRequest,
} from "@imbue/workspace-ui/src/models/request-error";
import { getChatById } from "../models/Chats";
import { addOutgoing, dropOutgoing, dropOutgoingNoTurnWillRetire } from "../models/OutgoingMessages";
import { accountForAgent } from "../models/Providers";
import { noteResentSend } from "../models/ResentSends";
import { mintMessageId, sendMessage, sentUserTurnIds } from "../models/Response";
import type { AssistantMessageEvent, TranscriptEvent, UserMessageEvent } from "../models/Response";
import { classifyUserMessage, isTurnUserMessage } from "./message-classification";

/** Whether the chat's account finished a sign-in after `event` failed on it. Only a failure of the
 *  chat's current agent counts: one an earlier agent of the chat wrote failed on that agent's account,
 *  and the message it refused went on with the switch. */
export function isSignedInSince(
  event: Pick<AssistantMessageEvent, "timestamp" | "agent_id">,
  chatId: string,
): boolean {
  const activeAgent = getChatById(chatId)?.active_agent;
  if (event.agent_id !== undefined && event.agent_id !== activeAgent?.agent_id) return false;
  const account = accountForAgent(activeAgent?.account_id ?? undefined);
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
      for (let index = start; index < end; index++) {
        const candidate = events[index];
        if (candidate.type !== "user_message" || !isTurnUserMessage(candidate)) continue;
        if (!isSentAfter(candidate, index, lastFailure.event, lastFailure.index)) prompts.push(candidate);
      }
      if (prompts.length > 0) {
        turns.push({ failures: failures.map((failure) => failure.event), prompts, isLast });
      }
    }
    start = end + 1;
  }
  return turns;
}

function wordsOf(prompt: UserMessageEvent): string {
  return classifyUserMessage(prompt).body.trim();
}

/** The user's messages sent after the turn's last failure, in transcript order: copies of what it
 *  refused, or whatever the user wrote next. */
function userTurnsSentSince(events: readonly TranscriptEvent[], turn: RefusedTurn): UserMessageEvent[] {
  const lastFailure = turn.failures[turn.failures.length - 1];
  const lastFailureIndex = events.indexOf(lastFailure);
  return events.filter(
    (event, index): event is UserMessageEvent =>
      event.type === "user_message" &&
      isTurnUserMessage(event) &&
      !turn.prompts.includes(event) &&
      isSentAfter(event, index, lastFailure, lastFailureIndex),
  );
}

/**
 * The copies of the turn's refused messages: the user's messages sent right after its last failure
 * that repeat one of them (the chat app's resend, "Send your message again", or a retype), up to the
 * first message that does not. The agent's replies in between do not end the run. A matching
 * message sent after the user wrote something else is a new message, not a copy.
 */
function copiesSentSince(sentSince: readonly UserMessageEvent[], turn: RefusedTurn): UserMessageEvent[] {
  const refusedWords = new Set(turn.prompts.map(wordsOf));
  const end = sentSince.findIndex((event) => !refusedWords.has(wordsOf(event)));
  return end === -1 ? [...sentSince] : sentSince.slice(0, end);
}

/**
 * The refused messages and failures to hide: a refused message once a copy of it was sent right
 * after the failure (see `copiesSentSince`), and the failures once every message they refused has
 * its copy. Nothing the user wrote is hidden while it exists only once.
 */
export function computeAuthErrorHiddenEventIds(events: readonly TranscriptEvent[]): Set<string> {
  const hidden = new Set<string>();
  for (const turn of refusedTurns(events)) {
    const copies = copiesSentSince(userTurnsSentSince(events, turn), turn);
    const copiedWords = new Set(copies.map(wordsOf));
    const copiedContents = new Set(copies.map((copy) => copy.content));
    // A prompt with no words of the user's (machine context alone) is never offered, but the chat app
    // sends it again as it was, so it is matched on what was sent.
    const copied = turn.prompts.filter((prompt) =>
      wordsOf(prompt) === "" ? copiedContents.has(prompt.content) : copiedWords.has(wordsOf(prompt)),
    );
    for (const prompt of copied) hidden.add(prompt.event_id);
    if (copied.length === turn.prompts.length) {
      for (const failure of turn.failures) hidden.add(failure.event_id);
    }
  }
  return hidden;
}

/**
 * The messages `failed` refused that have no copy in the chat yet, while the user has not moved on:
 * no sign-in failed again after it, and everything the user sent since is a copy of one of them
 * (a resend that stopped partway leaves the rest offered here). With no copy yet, the turn must
 * also still be the chat's last. Null otherwise.
 */
export function messagesToSendAgain(
  events: readonly TranscriptEvent[],
  failed: Pick<AssistantMessageEvent, "event_id">,
): UserMessageEvent[] | null {
  const turn = refusedTurns(events).find(
    (candidate) => candidate.failures[candidate.failures.length - 1].event_id === failed.event_id,
  );
  if (turn === undefined) return null;
  const lastFailureIndex = events.indexOf(turn.failures[turn.failures.length - 1]);
  // A later failure is a turn of its own, which offers its own messages.
  if (events.some((event, index) => index > lastFailureIndex && isAuthFailure(event))) return null;
  const sentSince = userTurnsSentSince(events, turn);
  const copies = copiesSentSince(sentSince, turn);
  if (copies.length < sentSince.length) return null;
  if (sentSince.length === 0 && !turn.isLast) return null;
  // A message typed again after an earlier failure of the turn is one message, offered once.
  const coveredWords = new Set(copies.map(wordsOf));
  const prompts: UserMessageEvent[] = [];
  for (const prompt of turn.prompts) {
    const words = wordsOf(prompt);
    if (words === "" || coveredWords.has(words)) continue;
    coveredWords.add(words);
    prompts.push(prompt);
  }
  return prompts.length > 0 ? prompts : null;
}

/** "unconfirmed": mngr typed the message and sent Enter but could not confirm the agent took it, or
 *  the chat app never answered the send, so the agent may already have it. */
export type ResendState = "sending" | "sent" | "failed" | "unconfirmed";

interface ResendRecord {
  state: ResendState;
  /** The message whose send could not be confirmed, for the "unconfirmed" state. */
  unconfirmedPromptId: string | null;
}

// Keyed by the failed turn's event id. Once the message lands in the transcript the failed turn
// is hidden, whatever its state here.
const resendStates = new Map<string, ResendRecord>();

// Keyed by the refused prompt's event id: each press sends it under the id its first press used, so
// the chat app answers a repeat of a send it took (one whose answer was lost) as it answered that one.
const resendMessageIds = new Map<string, string>();

function resendMessageIdOf(prompt: UserMessageEvent): string {
  const existing = resendMessageIds.get(prompt.event_id);
  if (existing !== undefined) return existing;
  const minted = mintMessageId();
  resendMessageIds.set(prompt.event_id, minted);
  return minted;
}

/** How the page's send of the turn's messages went, given the messages `offered` now. Once the
 *  message that could not be confirmed has landed it is no longer in doubt, and what is left of
 *  the turn simply did not send. */
export function resendStateOf(failedEventId: string, offered: readonly UserMessageEvent[] | null): ResendState | null {
  const record = resendStates.get(failedEventId);
  if (record === undefined) return null;
  if (record.state === "unconfirmed" && !offered?.some((prompt) => prompt.event_id === record.unconfirmedPromptId)) {
    return "failed";
  }
  return record.state;
}

function setResendState(failedEventId: string, state: ResendState, unconfirmedPromptId: string | null = null): void {
  resendStates.set(failedEventId, { state, unconfirmedPromptId });
  m.redraw();
}

/** Send `prompts` again for the turn `failedEventId`, each painted at once like any send. Each goes
 *  as it was first sent, machine context included: the agent never read any of it. */
export async function sendAgain(chatId: string, failedEventId: string, prompts: UserMessageEvent[]): Promise<void> {
  setResendState(failedEventId, "sending");
  for (const prompt of prompts) {
    const messageId = resendMessageIdOf(prompt);
    const outgoingId = addOutgoing(chatId, wordsOf(prompt), messageId);
    m.redraw();
    try {
      const priorTurnIds = sentUserTurnIds(chatId);
      const status = await sendMessage(chatId, prompt.content, messageId);
      dropOutgoingNoTurnWillRetire(chatId, outgoingId, status);
      // Delivered like any send, by a second one the chat app made when it could not confirm the first.
      if (status === "resent") noteResentSend(chatId, prompt.content, priorTurnIds);
      // Stop took it back while the agent came up: the user stopped the resend, so the rest stay unsent.
      if (status === "withdrawn") break;
    } catch (err) {
      console.error(`Failed to send the message again to chat ${chatId}: ${describeRequestError(err)}`);
      dropOutgoing(chatId, outgoingId);
      if (describeRequestErrorKind(err) === "submission_unconfirmed" || isUnansweredRequest(err)) {
        setResendState(failedEventId, "unconfirmed", prompt.event_id);
      } else {
        setResendState(failedEventId, "failed");
      }
      return;
    }
  }
  setResendState(failedEventId, "sent");
}

export function resetResendStatesForTesting(): void {
  resendStates.clear();
  resendMessageIds.clear();
}
