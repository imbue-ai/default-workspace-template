/**
 * The verbs the chat root's list offers on a chat, on the chat app's own routes. Each throws
 * with the server's detail on a refusal; the ``chats_updated`` push that follows carries the
 * result, so nothing here updates the list itself.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { HttpError, errorDetailFromResponse, postJson } from "@imbue/workspace-ui/src/models/http";

/** The chat app's kind for a rename refused because another chat has the name. */
const NAME_TAKEN_RENAME_REFUSAL_KIND = "name_taken";

/** A rename the chat app refused because another chat has the name; the message is its sentence. */
export class NameTakenError extends HttpError {}

function chatRoute(chatId: string, verb: string): string {
  return apiUrl(`/api/chats/${encodeURIComponent(chatId)}/${verb}`);
}

/** Throws ``NameTakenError`` for a name another chat has, and ``HttpError`` for any other refusal. */
export async function renameChat(chatId: string, title: string): Promise<void> {
  const response = await fetch(chatRoute(chatId, "rename"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
    cache: "no-store",
  });
  if (response.ok) return;
  const body = (await response
    .clone()
    .json()
    .catch(() => null)) as { kind?: unknown; detail?: unknown } | null;
  if (body?.kind === NAME_TAKEN_RENAME_REFUSAL_KIND && typeof body.detail === "string") {
    throw new NameTakenError(response.status, body.detail);
  }
  throw new HttpError(response.status, await errorDetailFromResponse(response));
}

/** Star the chat as the workspace's main chat, where a text with no chat of its own goes. */
export async function makeMainChat(chatId: string): Promise<void> {
  await postJson<unknown>(chatRoute(chatId, "make-main"), {});
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
