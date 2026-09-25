/**
 * The chat root: the chat list beside an inner frame of the selected chat, served by the chat
 * app at ``/`` (``/?chat=<id>`` selects). See docs/system/blueprint/desktop-interface/plan-desktop-interface.md
 * section 9.1 and docs/system/blueprint/post-launch-paths/plan-post-launch-paths.md section 3.6.
 *
 * The root owns the shell connection: it reports ``/?chat=<id>`` and the selected chat's
 * title as its location, handles ``shell:navigate`` by changing the selection, applies the
 * pending intake an ``intake=<token>`` in its URL names (a draft into a composer, a choice of
 * chat through the picker, or a first message that launches a chat through the provider
 * chooser) exactly once and then reports the selection alone, and drives its inner pages
 * directly (they share an origin) with the shell's handshake and its shown and hidden states,
 * so each page's presence reports key on the chat it shows. The inner pages' own ``minds:``,
 * ``shell:focused``, and sub-agent ``shell:open`` messages go up through ``relay.ts``; a page
 * asking for a sibling chat is answered here, by selecting it.
 */

import m from "mithril";
import "../style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection, ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { getBasePath } from "@imbue/workspace-ui/src/base-path";
import { adoptClientIdentity } from "@imbue/workspace-ui/src/models/ClientIdentity";
import {
  PendingIntakeGoneError,
  addChatsUpdatedListener,
  applyPendingIntake,
  createChat,
  discardPendingIntake,
  fetchPendingIntake,
  getChatById,
  getChats,
  getProvisionalChats,
  initChats,
  launchChat,
  removeChatsUpdatedListener,
} from "../models/Chats";
import type { AppliedIntake, PendingIntake } from "../models/Chats";
import {
  accountForAgent,
  closeProviderChooser,
  getSelectedAccount,
  isProviderChooserOpen,
  loadAccountsWithRetry,
  openProviderChooser,
} from "../models/Providers";
import { ProviderChooserModal } from "../views/ProviderChooserModal";
import { ChatRail } from "./ChatRail";
import type { ChatRailAttrs } from "./ChatRail";
import { SendPicker } from "./SendPicker";
import { initChatUnread, markRead, noteStatuses } from "./chatUnread";
import { InnerFramePool } from "./framePool";
import { startInnerFrameRelay } from "./relay";
import { groupedRows, rowsFromSnapshots } from "./rows";
import type { ChatRow } from "./rows";
import { intakeTokenFromSearch, rootPathFor, selectionFromSearch } from "./selection";
import { prependToComposer } from "../views/MessageInput";

// The desktop shell's compact breakpoint (desktop-interface contracts.md section 11): under
// it the list starts collapsed beside the chat, and fills the root while nothing is selected.
const COMPACT_MAX_WIDTH_PX = 700;
const ROOT_TITLE = "Chats";

let selectedChatId: string | null = null;
let connection: ShellConnection | null = null;
let handshake: ShellHandshake | null = null;
let isRootShown = true;
let pool: InnerFramePool | null = null;
// The intake the root's URL named and has not yet applied or given up (waiting for the chats, fetched, offered
// through the picker); null otherwise. While one is pending the shell holds the window at the token path it put it
// at, and the root reports no other location, so a reload or another client finds the token too.
let pendingToken: string | null = null;
// A held intake whose chat the user has to pick, while its picker is open; null otherwise.
let pendingPick: { token: string; intake: PendingIntake } | null = null;
// The chats started from this root: on top of the list until their first message.
const startedHere = new Set<string>();
// The chats this root created that no push has named yet. The socket's connect-time replay can land after the create
// returned, with a chat list from before it, and the chat's provisional record only follows that replay.
const awaitingListing = new Set<string>();
const compactQuery = window.matchMedia(`(max-width: ${COMPACT_MAX_WIDTH_PX}px)`);

function selectedTitle(): string {
  if (selectedChatId === null) return "";
  return getChatById(selectedChatId)?.title ?? "";
}

interface ReportedLocation {
  path: string;
  title: string;
}

// The last location told to the shell: every chat list push re-derives it, and only a change goes up.
let reportedLocation: ReportedLocation | null = null;

function reportLocation(): void {
  if (pendingToken !== null) return;
  const path = rootPathFor(selectedChatId);
  const title = selectedTitle();
  if (reportedLocation !== null && reportedLocation.path === path && reportedLocation.title === title) return;
  reportedLocation = { path, title };
  document.title = title || ROOT_TITLE;
  connection?.location(path, title);
}

/** Show ``chatId`` (or nothing): the URL, the frame, the shell's location, and the unread mark follow. */
function select(chatId: string | null): void {
  selectedChatId = chatId;
  history.replaceState(null, "", `${getBasePath()}${rootPathFor(chatId)}`);
  pool?.show(chatId);
  if (chatId !== null && isRootShown) markRead(chatId);
  reportLocation();
  m.redraw();
}

async function createAndSelect(accountId: string): Promise<void> {
  try {
    const created = await createChat("", accountId);
    startedHere.add(created.chatId);
    awaitingListing.add(created.chatId);
    select(created.chatId);
  } catch (error) {
    alert(`Failed to create chat: ${(error as Error).message}`);
  }
}

/** The New chat button: a chat on the selected account, or after a sign-in when nothing is signed in. */
function startNewChat(): void {
  const account = getSelectedAccount();
  if (account !== null) {
    void createAndSelect(account.id);
    return;
  }
  openProviderChooser({ onSignedIn: (signedInAccountId) => void createAndSelect(signedInAccountId) });
}

/** Show ``chatId`` once the pending intake is applied or given up, reporting the selection even when it is the one
 *  reported before the token path: the shell holds that path as this window's location until the root reports
 *  another. */
function settleIntake(chatId: string | null): void {
  pendingToken = null;
  reportedLocation = null;
  select(chatId);
}

/** Put ``text`` in a chat's composer, unsent: the live page's when it is loaded, else where the composer reads
 *  its persisted draft on mount. */
function draftInto(chatId: string, text: string): void {
  if (pool?.draftInto(chatId, text) === true) return;
  prependToComposer(chatId, text);
}

/** Launch a chat awaiting its first send with ``text`` (an intake that could not launch it at once): on the account
 *  the chat was minted for when it names one, else the signed-in account when one has appeared meanwhile, else
 *  through the provider chooser, as the composer's first send does; a dismissed chooser leaves the text in the
 *  composer, where the next send offers the chooser again. A chooser already open (for the New chat button) takes
 *  no second intent, so the text goes to the composer at once. */
function launchWithFirstMessage(chatId: string, text: string): void {
  const launchOrDraft = (accountId: string): void => {
    launchChat(chatId, accountId, text).catch((error: unknown) => {
      alert(`Failed to start the chat: ${(error as Error).message}`);
      draftInto(chatId, text);
    });
  };
  const minted = getProvisionalChats().find((chat) => chat.chat_id === chatId);
  const account = accountForAgent(minted?.account_id) ?? getSelectedAccount();
  if (account !== null) {
    launchOrDraft(account.id);
    return;
  }
  if (isProviderChooserOpen()) {
    draftInto(chatId, text);
    return;
  }
  openProviderChooser({ onSignedIn: launchOrDraft, onDismissed: () => draftInto(chatId, text) });
}

/** What an applied intake asks of the root (post-launch-paths plan section 3.6.1): the chat is selected, a draft
 *  goes into its composer, a first message launches it. */
function takeApplied(applied: AppliedIntake): void {
  startedHere.add(applied.chatId);
  settleIntake(applied.chatId);
  if (applied.composerText !== null) draftInto(applied.chatId, applied.composerText);
  if (applied.firstMessage !== null) launchWithFirstMessage(applied.chatId, applied.firstMessage);
}

/** Apply a held intake on the chat it resolved to, or on ``pickedChatId``; a token already gone (another client
 *  applied it, or it expired) leaves the chat shown as it stands, which for a token path naming a chat is that
 *  chat. */
async function applyIntake(token: string, pickedChatId: string | null): Promise<void> {
  try {
    takeApplied(await applyPendingIntake(token, pickedChatId));
  } catch (error) {
    if (!(error instanceof PendingIntakeGoneError)) alert(`Could not take the message: ${(error as Error).message}`);
    settleIntake(selectedChatId);
  }
}

/** The ``intake`` the root's URL carries: fetched, then applied at once, or offered through the picker when the
 *  chat is the user's to choose. Once applied or given up (a pick, a dismissal, a token already gone), the root
 *  reports the selection alone, so the window's stored path drops the token and a reload applies nothing again;
 *  while the picker is open the selection and the token path stand as they are. */
async function takeIntake(token: string): Promise<void> {
  let intake: PendingIntake;
  try {
    intake = await fetchPendingIntake(token);
  } catch (error) {
    if (!(error instanceof PendingIntakeGoneError)) alert(`Could not read the message: ${(error as Error).message}`);
    settleIntake(selectedChatId);
    return;
  }
  if (intake.needsPick) {
    pendingPick = { token, intake };
    m.redraw();
    return;
  }
  await applyIntake(token, null);
}

function pickFor(chatId: string): void {
  const pick = pendingPick;
  pendingPick = null;
  if (pick === null) return;
  void applyIntake(pick.token, chatId);
}

function dismissPick(): void {
  const pick = pendingPick;
  pendingPick = null;
  if (pick !== null) void discardPendingIntake(pick.token);
  settleIntake(selectedChatId);
}

function onChatsUpdated(): void {
  const rows = rowsFromSnapshots(getChats(), getProvisionalChats());
  noteStatuses(new Map(rows.map((row) => [row.chatId, row.status])), isRootShown ? selectedChatId : null);
  // A chat that has messaged is no longer new; a deleted chat's frame goes with it.
  for (const chatId of startedHere) {
    if (rows.some((row) => row.chatId === chatId && row.lastActiveMs !== null)) startedHere.delete(chatId);
  }
  const listed = new Set(rows.map((row) => row.chatId));
  for (const chatId of listed) awaitingListing.delete(chatId);
  const isKept = (chatId: string): boolean => listed.has(chatId) || awaitingListing.has(chatId);
  if (pool !== null) {
    for (const heldId of pool.heldChatIds()) {
      if (!isKept(heldId)) pool.destroy(heldId);
    }
  }
  if (selectedChatId !== null && !isKept(selectedChatId)) select(null);
  else reportLocation();
}

const ChatRoot: m.Component = {
  view() {
    const rows = groupedRows(rowsFromSnapshots(getChats(), getProvisionalChats()), startedHere);
    const isCompact = compactQuery.matches;
    // On a phone with nothing selected, the list is the whole page.
    const isListOnly = isCompact && selectedChatId === null;
    return m(
      "div",
      {
        class: "chat-root flex h-screen w-screen overflow-hidden bg-page",
        "data-compact": isCompact ? "true" : "false",
      },
      [
        isListOnly
          ? m("div", { class: "flex-1 min-w-0" }, m(ChatRail, railAttrs(rows, isCompact)))
          : m(ChatRail, railAttrs(rows, isCompact)),
        isListOnly
          ? null
          : m("div", { class: "chat-root-slot relative min-w-0 flex-1" }, [
              m("div", {
                class: "chat-root-frames absolute inset-0",
                // The frames are the pool's DOM, not mithril's: never reconciled.
                oncreate: ({ dom }: m.VnodeDOM) => {
                  pool = new InnerFramePool(dom as HTMLElement);
                  if (handshake !== null) pool.setHandshake(handshake);
                  pool.setRootShown(isRootShown);
                  pool.show(selectedChatId);
                },
                // The frames go with the container (the list alone on a phone); a new pool
                // is made when it comes back.
                onremove: () => {
                  pool = null;
                },
                onbeforeupdate: () => false,
              }),
              selectedChatId === null
                ? m(
                    "div",
                    {
                      class:
                        "chat-root-empty absolute inset-0 flex items-center justify-center text-(length:--font-size-body) text-secondary",
                    },
                    "Pick a chat, or start a new one.",
                  )
                : null,
            ]),
        isProviderChooserOpen() ? m(ProviderChooserModal, { onDismiss: closeProviderChooser }) : null,
        pendingPick === null
          ? null
          : m(SendPicker, {
              rows,
              text: pendingPick.intake.message,
              isDraft: pendingPick.intake.isDraft,
              onPick: pickFor,
              onDismiss: dismissPick,
            }),
      ],
    );
  },
};

function railAttrs(rows: readonly ChatRow[], isCompact: boolean): ChatRailAttrs {
  return {
    rows,
    selectedChatId,
    isCompact,
    onPick: (chatId: string) => select(chatId),
    onNew: () => startNewChat(),
  };
}

/** Run ``take`` once the chats have arrived (which chat an intake lands on is decided against them) and the
 *  accounts have loaded (a launch before they load would run on none). */
function onceListedAndAccountsLoaded(accountsLoaded: Promise<void>, take: () => void): void {
  const onceListed = (): void => {
    removeChatsUpdatedListener(onceListed);
    void accountsLoaded.then(take);
  };
  addChatsUpdatedListener(onceListed);
}

function connectRootToShell(accountsLoaded: Promise<void>): void {
  connection = connectToShell({
    capabilities: { navigation: true },
    onHandshake: (received) => {
      handshake = received;
      adoptClientIdentity({ clientId: received.clientId, desktopId: received.desktopId });
      pool?.setHandshake(received);
      // Whatever went up before the shell was listening is told again.
      reportedLocation = null;
      reportLocation();
      m.redraw();
    },
    onShown: () => {
      isRootShown = true;
      pool?.setRootShown(true);
      if (selectedChatId !== null) markRead(selectedChatId);
      m.redraw();
    },
    onHidden: () => {
      isRootShown = false;
      pool?.setRootShown(false);
      m.redraw();
    },
    onNavigate: (path) => {
      const target = new URL(path, window.location.origin);
      const token = intakeTokenFromSearch(target.search);
      const requested = selectionFromSearch(target.search);
      if (token !== null) {
        // The window is at the chat's path already; a picker path names none and leaves the shown chat standing.
        pendingToken = token;
        if (requested !== null) select(requested);
        void accountsLoaded.then(() => takeIntake(token));
        return;
      }
      select(requested);
    },
  });
  // Hidden until the shell says shown: the root can load into a background tab.
  if (connection.isFramed) isRootShown = false;
  window.addEventListener("focus", () => connection?.focused());
}

function bootstrap(): void {
  initChatUnread();
  initChats();
  const accountsLoaded = loadAccountsWithRetry();
  addChatsUpdatedListener(onChatsUpdated);
  compactQuery.addEventListener("change", () => m.redraw());
  connectRootToShell(accountsLoaded);
  startInnerFrameRelay(
    (source) => pool?.isInnerWindow(source) ?? false,
    (chatId) => select(chatId),
  );
  const rootElement = document.getElementById("app");
  if (rootElement === null) return;
  selectedChatId = selectionFromSearch(window.location.search);
  pendingToken = intakeTokenFromSearch(window.location.search);
  m.mount(rootElement, ChatRoot);
  reportLocation();
  const token = pendingToken;
  if (token !== null) onceListedAndAccountsLoaded(accountsLoaded, () => void takeIntake(token));
}

window.addEventListener("load", bootstrap);
