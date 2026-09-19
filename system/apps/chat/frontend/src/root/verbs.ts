/**
 * The verbs the chat root's list offers on a chat, on the chat app's own routes. Each throws
 * with the server's detail on a refusal; the ``chats_updated`` push that follows carries the
 * result, so nothing here updates the list itself.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { postJson } from "@imbue/workspace-ui/src/models/http";

function chatRoute(chatId: string, verb: string): string {
  return apiUrl(`/api/chats/${encodeURIComponent(chatId)}/${verb}`);
}

export async function renameChat(chatId: string, title: string): Promise<void> {
  await postJson<unknown>(chatRoute(chatId, "rename"), { title });
}

export async function stopChat(chatId: string): Promise<void> {
  await postJson<unknown>(chatRoute(chatId, "stop"), {});
}

export async function startChat(chatId: string): Promise<void> {
  await postJson<unknown>(chatRoute(chatId, "start"), {});
}

/** End the chat's agent and remove its conversation; it cannot be undone. */
export async function destroyChat(chatId: string): Promise<void> {
  await postJson<unknown>(chatRoute(chatId, "destroy"), {});
}
