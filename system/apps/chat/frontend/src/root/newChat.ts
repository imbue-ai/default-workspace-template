/**
 * The chat list's New chat button: a chat on the account the chat app picks, or with nothing signed in the chat
 * awaiting its first send, whose page asks for a provider before anything is created.
 */

import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";
import { CreateChatRefusedError, awaitingChatForEmptyList, createChat } from "../models/Chats";
import { getSelectedAccount, whenAccountsReadyToChoose } from "../models/Providers";

// The kind of a create the chat app refused because nothing is signed in (the server's
// ``NO_PROVIDER_ACCOUNT_CREATE_REFUSAL_KIND``).
const NO_PROVIDER_ACCOUNT_KIND = "no_provider_account";

/** What a click on New chat came to: a chat that is being created, the chat awaiting a provider, or the reason the
 *  chat app gave for starting neither. */
export type NewChatOutcome =
  | { kind: "created"; chatId: string }
  | { kind: "awaiting_provider"; chatId: string }
  | { kind: "refused"; detail: string };

export async function startNewChat(): Promise<NewChatOutcome> {
  await whenAccountsReadyToChoose();
  if (getSelectedAccount() !== null) {
    try {
      // No account named: the chat app picks it from its own account list as the create runs, so an account
      // signed out since this page read the list is never the one a new chat starts on.
      const created = await createChat("");
      return { kind: "created", chatId: created.chatId };
    } catch (error) {
      if (!(error instanceof CreateChatRefusedError) || error.kind !== NO_PROVIDER_ACCOUNT_KIND) {
        return { kind: "refused", detail: describeRequestError(error) };
      }
    }
  }
  try {
    return { kind: "awaiting_provider", chatId: await awaitingChatForEmptyList() };
  } catch (error) {
    return { kind: "refused", detail: describeRequestError(error) };
  }
}
