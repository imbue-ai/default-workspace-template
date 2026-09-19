/**
 * The chat root: the chat list beside an inner frame of the selected chat, served by the chat
 * app at ``/`` (``/?chat=<id>`` selects) and ``/new`` (the ``new`` launch path: a chat just
 * created and selected). See docs/system/blueprint/desktop-interface/plan-desktop-interface.md
 * section 9.1.
 *
 * The root owns the shell connection: it reports ``/?chat=<id>`` and the selected chat's
 * title as its location, handles ``shell:navigate`` by changing the selection, and drives its
 * inner pages directly (they share an origin) with the shell's handshake and its shown and
 * hidden states, so each page's presence reports key on the chat it shows. The inner pages'
 * own ``minds:``, ``shell:focused``, and ``shell:open`` messages go up through ``relay.ts``.
 */

import m from "mithril";
import "../style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection, ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { getBasePath } from "@imbue/workspace-ui/src/base-path";
import { adoptClientIdentity } from "@imbue/workspace-ui/src/models/ClientIdentity";
import {
  addChatsUpdatedListener,
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
import { ChatRail } from "./ChatRail";
import type { ChatRailAttrs } from "./ChatRail";
import { initChatUnread, markRead, noteStatuses } from "./chatUnread";
import { InnerFramePool } from "./framePool";
import { startInnerFrameRelay } from "./relay";
import { groupedRows, rowsFromSnapshots } from "./rows";
import type { ChatRow } from "./rows";
import { isNewChatPath, newChatParamsFromSearch, rootPathFor, selectionFromSearch } from "./selection";

// The desktop shell's compact breakpoint (desktop-interface contracts.md section 11): under
// it the list starts collapsed beside the chat, and fills the root while nothing is selected.
const COMPACT_MAX_WIDTH_PX = 700;
const ROOT_TITLE = "Chats";

let selectedChatId: string | null = null;
let connection: ShellConnection | null = null;
let handshake: ShellHandshake | null = null;
let isRootShown = true;
let pool: InnerFramePool | null = null;
// The chats started from this root: on top of the list until their first message.
const startedHere = new Set<string>();
const compactQuery = window.matchMedia(`(max-width: ${COMPACT_MAX_WIDTH_PX}px)`);

function selectedTitle(): string {
  if (selectedChatId === null) return "";
  return getChatById(selectedChatId)?.title ?? "";
}

function reportLocation(): void {
  document.title = selectedTitle() || ROOT_TITLE;
  connection?.location(rootPathFor(selectedChatId), selectedTitle());
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

async function createAndSelect(accountId: string, message: string): Promise<void> {
  try {
    const created = await createChat("", accountId, message);
    startedHere.add(created.chatId);
    select(created.chatId);
  } catch (error) {
    alert(`Failed to create chat: ${(error as Error).message}`);
  }
}

/** Start a new chat: on the account the user picked, or after a sign-in when nothing is signed in. */
function startNewChat(accountId: string, message: string): void {
  if (accountId !== "" || getSelectedAccount() !== null) {
    void createAndSelect(accountId !== "" ? accountId : (getSelectedAccount()?.id ?? ""), message);
    return;
  }
  openProviderChooser({ onSignedIn: (signedInAccountId) => void createAndSelect(signedInAccountId, message) });
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
        class: "chat-root flex h-screen w-screen overflow-hidden bg-bg",
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
      adoptClientIdentity({ clientId: received.clientId, deviceKind: received.deviceKind, viewId: received.viewId });
      pool?.setHandshake(received);
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
  void loadAccountsWithRetry();
  addChatsUpdatedListener(onChatsUpdated);
  compactQuery.addEventListener("change", () => m.redraw());
  connectRootToShell();
  startInnerFrameRelay((source) => pool?.isInnerWindow(source) ?? false);
  const rootElement = document.getElementById("app");
  if (rootElement === null) return;
  const isNew = isNewChatPath(window.location.pathname, getBasePath());
  selectedChatId = isNew ? null : selectionFromSearch(window.location.search);
  m.mount(rootElement, ChatRoot);
  reportLocation();
  if (isNew) {
    const params = newChatParamsFromSearch(window.location.search);
    // Accounts decide where the chat starts; a create before they load would run on none.
    void loadAccountsWithRetry().then(() => startNewChat(params.accountId, params.message));
  }
}

window.addEventListener("load", bootstrap);
