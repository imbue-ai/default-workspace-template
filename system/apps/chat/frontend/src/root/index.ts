/**
 * The chat root: the chat list beside an inner frame of the selected chat, served by the chat
 * app at ``/`` (``/?chat=<id>`` selects) and ``/new`` (the ``new`` launch path: a chat just
 * created and selected). See docs/system/blueprint/desktop-interface/plan-desktop-interface.md
 * section 9.1.
 *
 * The root owns the shell connection: it reports ``/?chat=<id>`` and the selected chat's
 * title as its location, handles ``shell:navigate`` by changing the selection (a ``draft`` in the
 * path goes, unsent, into a chat's composer; the ``send`` launch path opens the picker over the
 * chats and sends the text to the one picked), and drives its
 * inner pages directly (they share an origin) with the shell's handshake and its shown and
 * hidden states, so each page's presence reports key on the chat it shows. The inner pages'
 * own ``minds:``, ``shell:focused``, and sub-agent ``shell:open`` messages go up through
 * ``relay.ts``; a page asking for a sibling chat is answered here, by selecting it.
 */

import m from "mithril";
import "../style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection, ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { getBasePath } from "@imbue/workspace-ui/src/base-path";
import { adoptClientIdentity } from "@imbue/workspace-ui/src/models/ClientIdentity";
import {
  addChatsUpdatedListener,
  removeChatsUpdatedListener,
  createChat,
  getChatById,
  getChats,
  getProvisionalChats,
  initChats,
} from "../models/Chats";
import {
  closeProviderChooser,
  getSelectedAccount,
  isProviderChooserOpen,
  loadAccountsWithRetry,
  openProviderChooser,
} from "../models/Providers";
import { ProviderChooserModal } from "../views/ProviderChooserModal";
import { sendMessage } from "../models/Response";
import { ChatRail } from "./ChatRail";
import type { ChatRailAttrs } from "./ChatRail";
import { SendPicker, pickableRows } from "./SendPicker";
import { initChatUnread, markRead, noteStatuses } from "./chatUnread";
import { InnerFramePool } from "./framePool";
import { startInnerFrameRelay } from "./relay";
import { groupedRows, mostRecentChatId, rowsFromSnapshots } from "./rows";
import type { ChatRow } from "./rows";
import {
  draftFromSearch,
  isNewChatPath,
  isSendPath,
  newChatParamsFromSearch,
  rootPathFor,
  selectionFromSearch,
  sendTextFromSearch,
} from "./selection";
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
// The text the ``send`` launch path handed the root, while its picker is open; null otherwise.
let pendingSendText: string | null = null;
// The chats started from this root: on top of the list until their first message.
const startedHere = new Set<string>();
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

async function createAndSelect(accountId: string, message: string, then: (chatId: string) => void): Promise<void> {
  try {
    const created = await createChat("", accountId, message);
    startedHere.add(created.chatId);
    select(created.chatId);
    then(created.chatId);
  } catch (error) {
    alert(`Failed to create chat: ${(error as Error).message}`);
  }
}

/** Start a new chat: on the account the user picked, or after a sign-in when nothing is signed in; ``then`` runs
 *  with the chat once it is selected. */
function startNewChat(accountId: string, message: string, then: (chatId: string) => void = () => undefined): void {
  if (accountId !== "" || getSelectedAccount() !== null) {
    void createAndSelect(accountId !== "" ? accountId : (getSelectedAccount()?.id ?? ""), message, then);
    return;
  }
  openProviderChooser({
    onSignedIn: (signedInAccountId) => void createAndSelect(signedInAccountId, message, then),
  });
}

/** Put ``text`` in a chat's composer, unsent: the live page's when it is loaded, else where the composer reads
 *  its persisted draft on mount. */
function draftInto(chatId: string, text: string): void {
  if (pool?.draftInto(chatId, text) === true) return;
  prependToComposer(chatId, text);
}

/** The root's ``draft`` param (the desktop's "Design your own..."): the text goes to the composer of the chat the
 *  URL selects, else the shown one, else the most recently active one, else a chat created for it; the chat is
 *  selected and shown, and nothing is sent. The URL the root then reports carries the selection alone, so a reload
 *  does not draft again. */
function takeDraft(text: string, requestedChatId: string | null): void {
  // The shell holds the draft path as this window's location until the root reports another, so the
  // selection goes up even when it is the one already reported.
  reportedLocation = null;
  const rows = rowsFromSnapshots(getChats(), getProvisionalChats());
  const listed = new Set(rows.map((row) => row.chatId));
  const chatId =
    requestedChatId !== null && listed.has(requestedChatId)
      ? requestedChatId
      : selectedChatId !== null && listed.has(selectedChatId)
        ? selectedChatId
        : mostRecentChatId(rows, startedHere);
  if (chatId === null) {
    startNewChat("", "", (created) => draftInto(created, text));
    return;
  }
  select(chatId);
  draftInto(chatId, text);
}

/** The ``send`` launch path (launcher-and-getting-started plan section 4.5): with one chat to send to, the text goes
 *  there at once; with more, the picker opens over the list with the text, and picking a chat sends the text there
 *  through the ordinary send and selects it, while dismissing reports the selection alone. With no chat at all the
 *  text starts a new one, and an empty text is a no-op that reports the selection. Either way the window's stored
 *  path goes back to the selection, so a reload sends nothing again. */
function takeSend(text: string): void {
  reportedLocation = null;
  if (text === "") {
    select(selectedChatId);
    return;
  }
  const targets = pickableRows(rowsFromSnapshots(getChats(), getProvisionalChats()), "");
  if (targets.length === 0) {
    startNewChat("", text);
    return;
  }
  pendingSendText = text;
  if (targets.length === 1) {
    sendPendingTo(targets[0].chatId);
    return;
  }
  m.redraw();
}

function sendPendingTo(chatId: string): void {
  const text = pendingSendText;
  pendingSendText = null;
  if (text === null) return;
  select(chatId);
  sendMessage(chatId, text).catch((error: unknown) => {
    alert(`Failed to send the message: ${(error as Error).message}`);
  });
}

function dismissSend(): void {
  pendingSendText = null;
  select(selectedChatId);
}

function onChatsUpdated(): void {
  const rows = rowsFromSnapshots(getChats(), getProvisionalChats());
  noteStatuses(new Map(rows.map((row) => [row.chatId, row.status])), isRootShown ? selectedChatId : null);
  // A chat that has messaged is no longer new; a deleted chat's frame goes with it.
  for (const chatId of startedHere) {
    if (rows.some((row) => row.chatId === chatId && row.lastActiveMs !== null)) startedHere.delete(chatId);
  }
  if (pool !== null) {
    const listed = new Set(rows.map((row) => row.chatId));
    for (const heldId of pool.heldChatIds()) {
      if (!listed.has(heldId)) pool.destroy(heldId);
    }
  }
  if (selectedChatId !== null && !rows.some((row) => row.chatId === selectedChatId)) select(null);
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
        pendingSendText === null
          ? null
          : m(SendPicker, { rows, text: pendingSendText, onPick: sendPendingTo, onDismiss: dismissSend }),
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
    onNew: () => startNewChat("", ""),
  };
}

function connectRootToShell(): void {
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
      if (isNewChatPath(target.pathname, "")) {
        const params = newChatParamsFromSearch(target.search);
        startNewChat(params.accountId, params.message);
        return;
      }
      if (isSendPath(target.pathname, "")) {
        takeSend(sendTextFromSearch(target.search));
        return;
      }
      const draft = draftFromSearch(target.search);
      if (draft !== "") {
        takeDraft(draft, selectionFromSearch(target.search));
        return;
      }
      select(selectionFromSearch(target.search));
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
  connectRootToShell();
  startInnerFrameRelay(
    (source) => pool?.isInnerWindow(source) ?? false,
    (chatId) => select(chatId),
  );
  const rootElement = document.getElementById("app");
  if (rootElement === null) return;
  const isNew = isNewChatPath(window.location.pathname, getBasePath());
  const isSend = isSendPath(window.location.pathname, getBasePath());
  selectedChatId = isNew || isSend ? null : selectionFromSearch(window.location.search);
  m.mount(rootElement, ChatRoot);
  reportLocation();
  if (isNew) {
    const params = newChatParamsFromSearch(window.location.search);
    // Accounts decide where the chat starts; a create before they load would run on none.
    void accountsLoaded.then(() => startNewChat(params.accountId, params.message));
    return;
  }
  if (isSend) {
    // The picker lists the chats; opened before they load it would offer nothing.
    const text = sendTextFromSearch(window.location.search);
    const onceListedForSend = (): void => {
      removeChatsUpdatedListener(onceListedForSend);
      takeSend(text);
    };
    addChatsUpdatedListener(onceListedForSend);
    return;
  }
  const draft = draftFromSearch(window.location.search);
  if (draft !== "") {
    // The chats decide which composer takes it; a draft before they load would always start a new chat.
    const requested = selectedChatId;
    const onceListed = (): void => {
      removeChatsUpdatedListener(onceListed);
      void accountsLoaded.then(() => takeDraft(draft, requested));
    };
    addChatsUpdatedListener(onceListed);
  }
}

window.addEventListener("load", bootstrap);
