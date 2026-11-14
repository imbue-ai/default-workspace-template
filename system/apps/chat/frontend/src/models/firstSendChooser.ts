/**
 * The provider chooser a chat awaiting its first send asks for while nothing is signed in that it could start on.
 *
 * Accounts are the workspace's, so this chooser is about the workspace rather than one chat. The chat root opens it
 * over everything it frames, as soon as the chat it shows is in this state, or with no chat to show at all while
 * nothing is signed in (a new workspace whose welcome chat has not arrived), and keeps it open while its selection
 * changes (the seeded welcome chat arriving), so a sign-in already under way is neither lost nor redrawn. A chat page
 * opens its own only where no chat root frames it. Either way the sign-in it produces launches the chat waiting at
 * that moment, with no message, so its agent is up before the user has typed (the first send then carries a seeded
 * conversation), and a launch the chat app refuses lands on the page as a failed create, with its "Try again". Under
 * the root, a sign-in that lands with no such chat on screen launches the first one the root shows after it.
 */

import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";
import { getChatById, getProvisionalChat, isLaunchInFlight, launchChat } from "./Chats";
import { accountForFirstSend, areAccountsLoaded, getSelectedAccount, openProviderChooser } from "./Providers";

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

/** Whether the account list has loaded and holds nothing a new chat could start on. */
export function isNothingSignedIn(): boolean {
  return areAccountsLoaded() && getSelectedAccount() === null;
}

/** Whether ``chatId`` still awaits its first send with no launch of it out: a chat a sign-in can start. */
function isLaunchable(chatId: string): boolean {
  return (
    getProvisionalChat(chatId)?.phase === "awaiting_first_send" &&
    getChatById(chatId) === undefined &&
    !isLaunchInFlight(chatId)
  );
}

/** Launch ``chatId`` on the account a sign-in produced, with no message, while it still awaits its first send and no
 *  launch of it is out. */
export function launchOnSignIn(chatId: string, accountId: string): void {
  if (!isLaunchable(chatId)) return;
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

/** What the chat root tells its chooser. */
export interface RootFirstSendChooserDeps {
  /** The chat the root shows, or null while its slot is empty. */
  shownChatId: () => string | null;
  /** Whether the root is applying an intake, which decides the chat on screen and what starts it. */
  isIntakeUnderWay: () => boolean;
  /** The chooser's room row: the chat on screen, or with none a new one, becomes a room. */
  makeRoom: (chatId: string | null) => void;
}

/** The chat root's side: the chooser for whichever chat the root shows, or for the chat still to come. */
export interface RootFirstSendChooser {
  /** Open the chooser when the chat on screen awaits a provider, once per chat, so a dismissal stands until the
   *  chat's notice is pressed; with no chat on screen and nothing signed in, open it once for the chat still to come,
   *  and leave the first chat shown after that to that opening, open or dismissed. Then launch the chat a sign-in is
   *  waiting for, if it is on screen now. Run after every draw of the root. */
  afterDraw(): void;
  /** Open the chooser for the chat on screen, or for the chat still to come: its notice's, or the empty slot's,
   *  "Choose a provider". */
  open(): void;
  /** Whether a sign-in landed with no chat to start on screen, and waits to start the first one the root shows. */
  isLaunchPending(): boolean;
}

export function makeRootFirstSendChooser(deps: RootFirstSendChooserDeps): RootFirstSendChooser {
  const { shownChatId, isIntakeUnderWay, makeRoom } = deps;
  const openedFor = new Set<string>();
  let hasOpenedWithNoChat = false;
  // The opening with no chat on screen, until the first chat the root shows after it claims it.
  let isNoChatOpeningUnclaimed = false;
  // The account of a sign-in that found no chat on screen to start, until the root shows one.
  let pendingLaunchAccountId: string | null = null;

  const launchPendingIfShown = (): void => {
    const chatId = shownChatId();
    if (pendingLaunchAccountId === null || chatId === null || isIntakeUnderWay() || !isLaunchable(chatId)) return;
    const accountId = pendingLaunchAccountId;
    pendingLaunchAccountId = null;
    launchOnSignIn(chatId, accountId);
  };
  const open = (): void =>
    openProviderChooser({
      onSignedIn: (accountId) => {
        pendingLaunchAccountId = accountId;
        launchPendingIfShown();
      },
      onRoomChosen: () => makeRoom(shownChatId()),
    });
  const openIfAwaitingProvider = (): void => {
    const chatId = shownChatId();
    if (chatId === null) {
      if (hasOpenedWithNoChat || isIntakeUnderWay() || !isNothingSignedIn()) return;
      hasOpenedWithNoChat = true;
      isNoChatOpeningUnclaimed = true;
      open();
      return;
    }
    // A chat not yet pushed to this page (selected before its record arrived) cannot claim the opening yet.
    if (getChatById(chatId) === undefined && getProvisionalChat(chatId) === undefined) return;
    const isClaimingNoChatOpening = isNoChatOpeningUnclaimed;
    isNoChatOpeningUnclaimed = false;
    if (openedFor.has(chatId) || !isAwaitingProvider(chatId)) return;
    openedFor.add(chatId);
    if (!isClaimingNoChatOpening) open();
  };
  return {
    afterDraw: () => {
      openIfAwaitingProvider();
      launchPendingIfShown();
    },
    open,
    isLaunchPending: () => pendingLaunchAccountId !== null,
  };
}
