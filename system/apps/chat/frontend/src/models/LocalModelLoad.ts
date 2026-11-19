/**
 * Whether a chat's reply is waiting on its model server to read the model into memory.
 *
 * A model hosted on the user's computer is unloaded when idle, and its next reply waits for it to
 * be read off disk, which takes from seconds to minutes with nothing else to show for it. The
 * page asks the backend as the message is sent (``GET /api/chats/:id/model-load``), before the
 * send itself makes the server start loading, and the status row says so until the agent's
 * first work of the turn arrives.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { getChatById } from "./Chats";
import { effectiveChoice } from "./ModelSettings";

/** How a picker option names a model of the chat's own model server. */
const LOCAL_MODEL_OPTION_PREFIX = "local-model/";

interface PendingLoad {
  /** The turn that was the chat's latest when the load was noticed: undefined until the status row has looked,
   *  null when the chat had none. The load is over once a later turn shows the agent's work. */
  priorTurnId: string | null | undefined;
}

const pendingByChat = new Map<string, PendingLoad>();

function localModelOptionIdOf(chatId: string): string | null {
  const chat = getChatById(chatId);
  if (chat === undefined) return null;
  const optionId = effectiveChoice(chatId, chat.active_agent.model_choice)?.matched?.id ?? null;
  return optionId !== null && optionId.startsWith(LOCAL_MODEL_OPTION_PREFIX) ? optionId : null;
}

/** Ask, as a message is sent, whether its reply waits on the model being loaded. A chat on a provider asks nothing. */
export async function noteSendToModel(chatId: string): Promise<void> {
  pendingByChat.delete(chatId);
  const optionId = localModelOptionIdOf(chatId);
  if (optionId === null) return;
  try {
    const answer = await m.request<{ is_load_pending: boolean }>({
      method: "GET",
      url: apiUrl("/api/chats/:chatId/model-load"),
      params: { chatId, model: optionId },
    });
    if (answer.is_load_pending) pendingByChat.set(chatId, { priorTurnId: undefined });
  } catch {
    // Not knowing is the same as not loading: the status row keeps its usual words.
  }
}

/** The send did not go through, so no reply is on its way. */
export function forgetModelLoad(chatId: string): void {
  pendingByChat.delete(chatId);
}

/**
 * Whether the status row should say the model is loading, given the chat's latest turn (its opening
 * event's id, and whether the agent has recorded work in it). Ends the wait once a turn later than
 * the one the load was noticed in shows the agent's work.
 */
export function isModelLoadPending(chatId: string, latestTurn: { id: string; hasAgentWork: boolean } | null): boolean {
  const pending = pendingByChat.get(chatId);
  if (pending === undefined) return false;
  if (pending.priorTurnId === undefined) {
    // A turn that already shows work is the one before this send; one with none yet is the send's own.
    pending.priorTurnId = latestTurn !== null && latestTurn.hasAgentWork ? latestTurn.id : null;
  }
  const isReplyStarted = latestTurn !== null && latestTurn.hasAgentWork && latestTurn.id !== pending.priorTurnId;
  if (isReplyStarted) pendingByChat.delete(chatId);
  return !isReplyStarted;
}
