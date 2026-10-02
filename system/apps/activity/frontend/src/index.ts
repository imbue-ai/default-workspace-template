/**
 * The Activity page's root: it connects to the shell that frames it, holds the Memory and Storage tabs, and reads
 * only what the visible tab needs. The memory tab re-reads every few seconds, and its history once a minute, while
 * its window is shown (``shell:shown`` / ``shell:hidden``; outside the shell, while the browser tab is visible); the
 * storage tab measures once when first opened and again only when asked.
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
import { getStorageState, measureStorage } from "./models/storage";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import {
  getRefreshFailure,
  getSummaryState,
  refreshNow,
  requestAppStop,
  requestChatAction,
  serverNowMs,
  startRefreshing,
  stopRefreshing,
} from "./models/summary";
import { ActivityPage } from "./views/ActivityPage";
import { StoragePage } from "./views/StoragePage";

export const PAGE_PATH = "/";
export const PAGE_TITLE = "System Monitor";

type Tab = "memory" | "storage";

const TAB_LABELS: Record<Tab, string> = { memory: "Memory", storage: "Storage" };
const TAB_PANEL_ID = "activity-tab-panel";

function tabId(tab: Tab): string {
  return `activity-tab-${tab}`;
}

function bootstrap(): void {
  let handshake: ShellHandshake | null = null;
  let tab: Tab = "memory";
  // A window starts shown. The shell says when its window is hidden; the browser tab being in the background is a
  // second way to be out of sight, framed or not.
  let isWindowShown = true;

  function syncRefreshing(): void {
    if (isWindowShown && document.visibilityState === "visible" && tab === "memory") {
      startRefreshing();
      startHistoryRefreshing();
    } else {
      stopRefreshing();
      stopHistoryRefreshing();
    }
  }

  function selectTab(next: Tab): void {
    tab = next;
    if (tab === "storage" && getStorageState().kind === "idle") void measureStorage();
    syncRefreshing();
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

  const tabs = Object.keys(TAB_LABELS) as Tab[];

  /** Arrow keys move between the tabs, as the WAI-ARIA tabs pattern has it, selecting the one they land on. */
  function onTabKey(event: KeyboardEvent): void {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (step === 0) return;
    event.preventDefault();
    const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
    selectTab(next);
    m.redraw.sync();
    document.getElementById(tabId(next))?.focus();
  }

  const rootElement = document.getElementById("app");
  if (rootElement === null) return;
  m.mount(rootElement, {
    view: () =>
      m("div", { class: "activity-page h-screen w-full overflow-y-auto bg-page" }, [
        m("div", { class: "mx-auto flex max-w-[760px] flex-col gap-6 px-6 py-6" }, [
          m(
            "div",
            {
              class: "flex gap-1 border-b border-default pb-2",
              role: "tablist",
              "aria-label": PAGE_TITLE,
              onkeydown: onTabKey,
            },
            tabs.map((key) =>
              m(
                Button,
                {
                  key,
                  variant: "ghost",
                  sm: true,
                  selected: tab === key,
                  extra: "activity-tab",
                  id: tabId(key),
                  role: "tab",
                  "aria-selected": String(tab === key),
                  "aria-controls": TAB_PANEL_ID,
                  tabindex: tab === key ? 0 : -1,
                  onclick: () => selectTab(key),
                },
                TAB_LABELS[key],
              ),
            ),
          ),
          m("div", { id: TAB_PANEL_ID, role: "tabpanel", "aria-labelledby": tabId(tab) }, [
            tab === "memory"
              ? m(ActivityPage, {
                  state: getSummaryState(),
                  refreshFailure: getRefreshFailure(),
                  nowMs: serverNowMs(),
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
                  onAppStop: async (appName) => {
                    const result = await requestAppStop(appName);
                    await refreshNow();
                    return result;
                  },
                  history: { state: getHistoryState(), range: getSelectedRange(), onRange: selectRange },
                })
              : m(StoragePage, { state: getStorageState(), onMeasure: () => void measureStorage() }),
          ]),
        ]),
      ]),
  });
}

window.addEventListener("load", bootstrap);
