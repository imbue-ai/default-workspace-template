/**
 * The Activity page's root: it connects to the shell that frames it and reads only while it is seen. The memory tab
 * re-reads every few seconds, and its history once a minute, while its window is shown (``shell:shown`` /
 * ``shell:hidden``) and the browser tab is visible.
 */

import m from "mithril";
import "./style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import {
  getHistoryState,
  getSelectedRange,
  selectRange,
  startHistoryRefreshing,
  stopHistoryRefreshing,
} from "./models/history";
import {
  getRefreshFailure,
  getSummaryState,
  refreshNow,
  requestChatAction,
  startRefreshing,
  stopRefreshing,
} from "./models/summary";
import { ActivityPage } from "./views/ActivityPage";

export const PAGE_PATH = "/";
export const PAGE_TITLE = "System Monitor";

function bootstrap(): void {
  let handshake: ShellHandshake | null = null;
  // A window starts shown. The shell says when its window is hidden; the browser tab being in the background is a
  // second way to be out of sight, framed or not.
  let isWindowShown = true;

  function syncRefreshing(): void {
    if (isWindowShown && document.visibilityState === "visible") {
      startRefreshing();
      startHistoryRefreshing();
    } else {
      stopRefreshing();
      stopHistoryRefreshing();
    }
  }

  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(PAGE_PATH, PAGE_TITLE);
    },
    onShown: () => {
      isWindowShown = true;
      syncRefreshing();
    },
    onHidden: () => {
      isWindowShown = false;
      syncRefreshing();
    },
  });
  window.addEventListener("focus", () => connection.focused());
  installElementContextMenu({ connection, handshake: () => handshake, open: createContextMenuOpener().open });
  document.addEventListener("visibilitychange", syncRefreshing);
  syncRefreshing();

  const rootElement = document.getElementById("app");
  if (rootElement === null) return;
  m.mount(rootElement, {
    view: () =>
      m("div", { class: "activity-page h-screen w-full overflow-y-auto bg-page" }, [
        m("div", { class: "mx-auto flex max-w-[760px] flex-col gap-6 px-6 py-6" }, [
          m(ActivityPage, {
            state: getSummaryState(),
            refreshFailure: getRefreshFailure(),
            nowMs: Date.now(),
            onAskInChat: (text) => {
              if (!connection.isFramed) return false;
              connection.draftText(text);
              return true;
            },
            onChatAction: async (chatId, action, isInterruptConfirmed) => {
              const result = await requestChatAction(chatId, action, isInterruptConfirmed);
              await refreshNow();
              return result;
            },
            history: { state: getHistoryState(), range: getSelectedRange(), onRange: selectRange },
          }),
        ]),
      ]),
  });
}

window.addEventListener("load", bootstrap);
