/**
 * Asking for a harness switch, and knowing whether one is running.
 *
 * A switch is a BACKEND operation reported on the agents WebSocket, so this module holds
 * almost nothing: the running switch arrives as ``handoff`` on whichever agent currently
 * backs the chat, and every client sees it, not just the one that asked. What is kept here
 * is the two things the WebSocket cannot supply.
 *
 * The first is the gap between the click and the first push. The POST returns as soon as the
 * switch is accepted, and the card has to say something in the meantime, so an accepted
 * request is remembered locally and read as ``preparing`` until the backend's own report
 * takes over. It is dropped once the backend reports the switch, or once the chat is
 * observed already running on the harness it was moving to -- a switch that finished before
 * any push reached us would otherwise leave the card claiming to be busy forever.
 *
 * The second is a FAILURE the user has read. Every reason a switch cannot start is decided
 * synchronously by the backend and returned as the response body (mid-turn, unsent queued
 * messages, a switch already running, an account this build cannot bind); a switch that breaks
 * after starting is reported as a ``failed`` phase that stays in the payload until the next
 * switch overwrites it. Both are shown verbatim -- those sentences are the user's answer, and
 * the frontend deliberately keeps no copy of the eligibility rules -- and both are dismissible
 * here rather than backend-side, because a failed switch leaves the chat exactly as it was:
 * there is nothing to clear but the telling.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { chatIdOfAgent } from "./AgentManager";
import type { AgentState, HandoffState } from "./AgentManager";
import { describeRequestError } from "./request-error";

interface AcceptedSwitch {
  operationId: string;
  targetHarness: string;
}

// Switches this client has had accepted but not yet seen reported. Keyed by chat id.
const acceptedByChat = new Map<string, AcceptedSwitch>();
// The last refusal for a chat, until the user dismisses it or asks again.
const refusalByChat = new Map<string, string>();
// The reported failure a user has already read, by the text they read. Keyed on the text so a
// LATER failure of the same chat still shows: two switches never fail identically by accident.
const dismissedByChat = new Map<string, string>();

/** The idempotency key for one switch. A retry of the same click carries the same key, so the
 *  backend can join the running switch rather than start a second one. */
function mintOperationId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `switch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Ask the backend to move this chat onto ``accountId``'s harness.
 *
 * Fire-and-forget by design: the reply says only that the switch was accepted, and the switch
 * itself reports through the agents WebSocket. ``targetHarness`` is what the card should say
 * while the first push is in flight, and is also how a switch that completed unobserved is
 * recognised later.
 */
export function requestHarnessSwitch(chatId: string, accountId: string, targetHarness: string): void {
  refusalByChat.delete(chatId);
  dismissedByChat.delete(chatId);
  const operationId = mintOperationId();
  acceptedByChat.set(chatId, { operationId, targetHarness });
  m.redraw();
  void m
    .request<{ status: string; operation_id: string }>({
      method: "POST",
      url: apiUrl(`/api/chats/${encodeURIComponent(chatId)}/switch-harness`),
      body: { account_id: accountId, operation_id: operationId },
    })
    .catch((error: unknown) => {
      // A refusal is the normal way this fails, and the backend's own sentence says whether
      // waiting would help -- so it is shown verbatim rather than summarised.
      acceptedByChat.delete(chatId);
      refusalByChat.set(chatId, describeRequestError(error));
      m.redraw();
    });
}

/**
 * The switch running on this chat, or null when none is.
 *
 * The backend's report wins whenever there is one; a locally accepted switch only covers the
 * round trip before the first push.
 */
export function harnessSwitchFor(agent: AgentState | undefined): HandoffState | null {
  if (agent === undefined) return null;
  const chatId = chatIdOfAgent(agent);
  const reported = agent.handoff ?? null;
  if (reported !== null) {
    acceptedByChat.delete(chatId);
    return reported;
  }
  const accepted = acceptedByChat.get(chatId);
  if (accepted === undefined) return null;
  // Already there: the switch landed without this client ever seeing it in flight.
  if (agent.harness === accepted.targetHarness) {
    acceptedByChat.delete(chatId);
    return null;
  }
  return { phase: "preparing", target_harness: accepted.targetHarness };
}

/** Whether this chat is mid-switch, and so cannot be asked to switch again. */
export function isSwitchingHarness(agent: AgentState | undefined): boolean {
  const state = harnessSwitchFor(agent);
  return state !== null && state.phase !== "failed";
}

/**
 * What to tell the user about this chat's last switch attempt, or null when there is nothing
 * to tell: a refusal this client was handed, or a failure the backend reported and nobody has
 * read yet. One accessor for both, because to the user they are one thing -- the switch did not
 * happen, and here is why.
 */
export function harnessSwitchFailureFor(agent: AgentState | undefined): string | null {
  if (agent === undefined) return null;
  const chatId = chatIdOfAgent(agent);
  const refusal = refusalByChat.get(chatId);
  if (refusal !== undefined) return refusal;
  const reported = agent.handoff ?? null;
  if (reported === null || reported.phase !== "failed") return null;
  const detail = reported.detail ?? "";
  if (detail === "" || dismissedByChat.get(chatId) === detail) return null;
  return detail;
}

/** Stop telling the user about the last failure. Local, because a failed switch left the chat
 *  exactly as it was -- there is no backend state to undo, only a message to stop showing. */
export function dismissHarnessSwitchFailure(agent: AgentState): void {
  const chatId = chatIdOfAgent(agent);
  refusalByChat.delete(chatId);
  const reported = agent.handoff ?? null;
  if (reported !== null && reported.phase === "failed") {
    dismissedByChat.set(chatId, reported.detail ?? "");
  }
  m.redraw();
}
