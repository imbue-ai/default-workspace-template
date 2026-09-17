/**
 * The handoff routes (spec 5.2): switch a chat to another account, call the switch off, or
 * retry a failed one. What the switch is doing shows up on the chat's snapshot (``handoff``),
 * not in these answers.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { getActiveProjectId, getClientId, getDeviceKind } from "@imbue/workspace-ui/src/models/ClientIdentity";
import type { HandoffPhase, TransitionKind } from "./Chats";
import type { ModelIdentity } from "./ModelSettings";
import { announceMessageSent } from "./Response";

export interface SwitchChatResult {
  /** Whether the target made the switch a handoff (a new agent) or a rebind (the same agent, another account). */
  kind: TransitionKind;
  phase: HandoffPhase;
  /** The queued text taken off the agent the chat is leaving, for the composer ("" for none). */
  returned_block: string;
}

/** Move the chat to ``accountId`` with ``message`` as the new agent's first message ("" for a switch
 *  made with nothing to say yet) and, for a handoff, ``pick`` as the model it runs on (null for the
 *  harness's default). The client fields ride along as on a send, so the activity report names this
 *  browser. */
export async function switchChat(
  chatId: string,
  accountId: string,
  message: string,
  messageId: string,
  pick: ModelIdentity | null = null,
): Promise<SwitchChatResult> {
  announceMessageSent(chatId);
  return await m.request<SwitchChatResult>({
    method: "POST",
    url: apiUrl("/api/chats/:chatId/handoff"),
    params: { chatId },
    body: {
      account_id: accountId,
      message,
      message_id: messageId,
      model: pick,
      client_id: getClientId(),
      active_layout: getActiveProjectId(),
      device_kind: getDeviceKind(),
    },
  });
}

/** Call the switch off while that is still possible; the confirming message comes back for the composer. */
export async function cancelHandoff(chatId: string): Promise<{ returned_block: string }> {
  return await m.request<{ status: string; returned_block: string }>({
    method: "POST",
    url: apiUrl("/api/chats/:chatId/handoff/cancel"),
    params: { chatId },
  });
}

/** Run a failed switch's create again on ``accountId``, any signed-in account. */
export async function retryHandoff(chatId: string, accountId: string): Promise<{ phase: HandoffPhase }> {
  return await m.request<{ status: string; phase: HandoffPhase }>({
    method: "POST",
    url: apiUrl("/api/chats/:chatId/handoff/retry"),
    params: { chatId },
    body: { account_id: accountId },
  });
}
