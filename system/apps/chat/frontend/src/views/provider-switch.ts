/**
 * "Switch provider": the way out of a failure the provider or the account is behind. It opens the provider chooser
 * with the failing account listed but refused, so every surface that offers it marks that account the same way.
 */

import { getChatById } from "../models/Chats";
import { openProviderChooser } from "../models/Providers";
import { beginSwitchToAccountId } from "./SwitchDialog";

export const SWITCH_PROVIDER_LABEL = "Switch provider";

/** Open the provider chooser to pick an account in place of ``failingAccountId``, which it lists but refuses (none
 *  when empty). ``onChosen`` takes the account signed in to or picked. */
export function chooseProviderInPlaceOf(failingAccountId: string, onChosen: (accountId: string) => void): void {
  openProviderChooser({
    onSignedIn: onChosen,
    ...(failingAccountId ? { unpickable: { accountId: failingAccountId, reason: "failing" as const } } : {}),
  });
}

/** Move ``chatId`` off the account it runs on: a rebind for another account on the same harness and lane, a handoff
 *  otherwise. */
export function chooseProviderToSwitchChat(chatId: string): void {
  chooseProviderInPlaceOf(getChatById(chatId)?.active_agent.account_id ?? "", (chosen) =>
    beginSwitchToAccountId(chatId, chosen),
  );
}
