/**
 * The provider chooser a chat awaiting its first send asks for while nothing is signed in that it could start on.
 *
 * Accounts are the workspace's, so this chooser is about the workspace rather than one chat. The chat root opens it
 * over everything it frames, as soon as the chat it shows is in this state, and keeps it open while its selection
 * changes (the seeded welcome chat arriving after the chat an empty list opened), so a sign-in already under way is
 * neither lost nor redrawn. A chat page opens its own only where no chat root frames it. Either way the sign-in it
 * produces launches the chat waiting at that moment, with no message, so its agent is up before the user has typed
 * (the first send then carries a seeded conversation), and a launch the chat app refuses lands on the page as a
 * failed create, with its "Try again".
 */

import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";
import { getChatById, getProvisionalChat, isLaunchInFlight, launchChat } from "./Chats";
import { accountForFirstSend, areAccountsLoaded, openProviderChooser } from "./Providers";

/** Whether ``chatId`` awaits its first send with no account to start on: the state the chooser is for. False until
 *  the account list has loaded, when no account reads as missing. */
export function isAwaitingProvider(chatId: string): boolean {
  const awaiting = getChatById(chatId) === undefined ? getProvisionalChat(chatId) : undefined;
  return (
    awaiting?.phase === "awaiting_first_send" &&
    areAccountsLoaded() &&
    accountForFirstSend(awaiting.account_id) === null
  );
}

/** Launch ``chatId`` on the account a sign-in produced, with no message, while it still awaits its first send and no
 *  launch of it is out. */
export function launchOnSignIn(chatId: string, accountId: string): void {
  if (getProvisionalChat(chatId)?.phase !== "awaiting_first_send" || getChatById(chatId) !== undefined) return;
  if (isLaunchInFlight(chatId)) return;
  launchChat(chatId, accountId, "", "agent").catch((error: unknown) => {
    console.error(`The chat ${chatId} could not be started on sign-in: ${describeRequestError(error)}`);
  });
}

/** Open the chooser for the chat ``waitingChatId`` names when a sign-in or the room row lands (null when none does):
 *  read then rather than now, so the chat that is waiting when the user finishes is the one launched. */
export function openFirstSendChooser(waitingChatId: () => string | null, makeRoom: (chatId: string) => void): void {
  openProviderChooser({
    onSignedIn: (accountId) => {
      const chatId = waitingChatId();
      if (chatId !== null) launchOnSignIn(chatId, accountId);
    },
    onRoomChosen: () => {
      const chatId = waitingChatId();
      if (chatId !== null) makeRoom(chatId);
    },
  });
}

/** The chat root's side: the chooser for whichever chat the root shows. */
export interface RootFirstSendChooser {
  /** Open the chooser when the chat on screen awaits a provider, once per chat, so a dismissal stands until the
   *  chat's notice is pressed. Run after every draw of the root. */
  openIfAwaitingProvider(): void;
  /** Open the chooser for the chat on screen: its notice's "Choose a provider". */
  open(): void;
}

export function makeRootFirstSendChooser(
  shownChatId: () => string | null,
  makeRoom: (chatId: string) => void,
): RootFirstSendChooser {
  const openedFor = new Set<string>();
  const open = (): void => openFirstSendChooser(shownChatId, makeRoom);
  return {
    openIfAwaitingProvider: () => {
      const chatId = shownChatId();
      if (chatId === null || openedFor.has(chatId) || !isAwaitingProvider(chatId)) return;
      openedFor.add(chatId);
      open();
    },
    open,
  };
}
