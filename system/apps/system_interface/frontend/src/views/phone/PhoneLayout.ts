/**
 * The phone layout (plan-phone-interface.md): the update banners, then the page host (the one shown window's
 * page, or the home grid over it), the toasts, and the bar; the windows and start sheets rise over all of it,
 * and a window's menu or an app's launch rows open as the desktop's menus do. What it shows is the store's
 * (``state.phone``); the sheets' text and highlight and which menu is open are this view's own.
 *
 * The layout is sized to the visual viewport rather than the layout one, so the bar rides above a soft keyboard
 * instead of under it.
 */

import m from "mithril";
import { createMenu } from "@imbue/workspace-ui/src/components/menu";
import type { MenuRow } from "@imbue/workspace-ui/src/components/menu";
import { appLaunchesOf, launchRowKindOf } from "../../model/launch";
import { appByName, isAppShownStopped } from "../../reducers/desktopState";
import { defaultHighlightIndex, isRowEnabled, launcherRowsOf } from "../../reducers/launcherRows";
import type { LauncherRow } from "../../reducers/launcherRows";
import {
  homeGridApps,
  openWindowCount,
  phonePillOf,
  pinnedChatWindowOf,
  shownWindowOf,
  windowsSheetRows,
} from "../../reducers/phone";
import type { DesktopStore } from "../../store/DesktopStore";
import { Toasts } from "../Toast";
import { UpdateNoticeBanner } from "../UpdateNoticeBanner";
import { UpdateStalenessBanner } from "../UpdateStalenessBanner";
import { stoppedPlaceholder } from "../Window";
import { openShareSettings, phoneWindowMenuRows } from "../WindowMenu";
import { HomeGrid } from "./HomeGrid";
import { PhoneBar } from "./PhoneBar";
import { StartSheet } from "./StartSheet";
import { WindowsSheet } from "./WindowsSheet";

/** The width the phone's menus never go under, as the desktop's. */
const MENU_MIN_WIDTH = 176;

type PhoneMenu =
  { readonly kind: "window"; readonly windowId: string } | { readonly kind: "app"; readonly app: string };

export interface PhoneLayoutAttrs {
  readonly store: DesktopStore;
  /** The element the live pages are appended to, created once and never re-rendered. */
  readonly onPagesHostCreated: (host: HTMLElement) => void;
}

/** Whether a chrome embeds this page (the Imbue Studio app), which is where a share surface exists. */
function isEmbedded(): boolean {
  return window.parent !== window;
}

/** The count a Close all confirms. */
export function closeAllPrompt(count: number): string {
  return count === 1 ? "Close 1 window?" : `Close ${count} windows?`;
}

export function PhoneLayout(): m.Component<PhoneLayoutAttrs> {
  let openMenu: PhoneMenu | null = null;
  let windowQuery = "";
  let startQuery = "";
  let startHighlight: number | null = null;
  let root: HTMLElement | null = null;
  const menu = createMenu({
    placement: "below",
    role: "menu",
    minWidth: MENU_MIN_WIDTH,
    get extraClass(): string | undefined {
      return openMenu === null ? undefined : `phone-${openMenu.kind}-menu`;
    },
    onClose: () => {
      openMenu = null;
    },
  });

  function fitToVisualViewport(): void {
    const viewport = window.visualViewport;
    if (root === null || !viewport) return;
    root.style.top = `${viewport.offsetTop}px`;
    root.style.height = `${viewport.height}px`;
  }

  function openMenuAt(next: PhoneMenu, target: HTMLElement): void {
    openMenu = next;
    menu.open(target);
    m.redraw();
  }

  function rowsOfWindowMenu(store: DesktopStore, windowId: string): MenuRow[] | null {
    const state = store.getState();
    const found = state.desktops.flatMap((desktop) => desktop.windows).find((window) => window.id === windowId);
    if (found === undefined) return null;
    const app = appByName(state, found.app);
    return phoneWindowMenuRows(app, {
      refresh: () => store.refreshWindow(windowId),
      share: app === undefined || app.critical || !isEmbedded() ? null : () => openShareSettings(app),
      quit: app !== undefined && store.canStopApp(app) ? () => void store.quitApp(app.name) : null,
    });
  }

  /** An app's launch rows, each opening a new window (the focus row of a pinned app shows its window). */
  function rowsOfAppMenu(store: DesktopStore, appName: string): MenuRow[] | null {
    const app = appByName(store.getState(), appName);
    if (app === undefined) return null;
    return appLaunchesOf([app])
      .filter(({ launchPath }) => launchRowKindOf(app, launchPath) !== "text")
      .map(({ launchPath }) => ({
        kind: "action",
        key: `launch-${launchPath.id}`,
        label: launchPath.label,
        onSelect: () => void store.runLaunchRow(app.name, launchPath.id),
      }));
  }

  function startRows(store: DesktopStore): readonly LauncherRow[] {
    return launcherRowsOf(store.getState(), startQuery).rows;
  }

  function startHighlightIndex(rows: readonly LauncherRow[]): number {
    const moved = startHighlight;
    if (moved !== null && moved >= 0 && moved < rows.length && isRowEnabled(rows[moved])) return moved;
    return defaultHighlightIndex(rows);
  }

  /** Run a start-sheet row: the sheet closes and its text clears first, whatever the row does. */
  function runStartRow(store: DesktopStore, row: LauncherRow): void {
    store.openPhoneSheet(null);
    startQuery = "";
    startHighlight = null;
    switch (row.kind) {
      case "launch":
        void store.runLaunchRow(row.app.name, row.launchPath.id);
        return;
      case "window":
        store.showOnPhone({ kind: "window", windowId: row.window.id });
        return;
      case "text":
        void store.runFreeText(row.app.name, row.launchPath.id, row.text);
        return;
    }
  }

  function sheetView(store: DesktopStore): m.Children {
    const state = store.getState();
    switch (state.phone.sheet) {
      case null:
        return null;
      case "windows":
        return m(WindowsSheet, {
          rows: windowsSheetRows(state),
          shownWindowId: shownWindowOf(state)?.id ?? null,
          avatar: state.avatar,
          query: windowQuery,
          onQuery: (query) => {
            windowQuery = query;
          },
          onShow: (windowId) => store.showOnPhone({ kind: "window", windowId }),
          onClose: (windowId) => void store.closeWindow(windowId),
          onMenu: (windowId, target) => openMenuAt({ kind: "window", windowId }, target),
          onCloseAll: () => {
            const count = openWindowCount(state);
            if (count > 0 && window.confirm(closeAllPrompt(count))) void store.closeAllWindows();
          },
          onDismiss: () => store.openPhoneSheet(null),
        });
      case "start": {
        const menuRows = launcherRowsOf(state, startQuery);
        return m(StartSheet, {
          menu: menuRows,
          highlightIndex: startHighlightIndex(menuRows.rows),
          query: startQuery,
          onQuery: (query) => {
            startQuery = query;
            startHighlight = null;
          },
          onRun: (row) => runStartRow(store, row),
          onHighlight: (index) => {
            startHighlight = index;
          },
          onRunHighlight: () => {
            const rows = startRows(store);
            const index = startHighlightIndex(rows);
            if (index >= 0) runStartRow(store, rows[index]);
          },
          onDismiss: () => store.openPhoneSheet(null),
        });
      }
    }
  }

  return {
    oncreate(vnode) {
      root = vnode.dom as HTMLElement;
      window.visualViewport?.addEventListener("resize", fitToVisualViewport);
      window.visualViewport?.addEventListener("scroll", fitToVisualViewport);
      fitToVisualViewport();
    },
    onremove() {
      window.visualViewport?.removeEventListener("resize", fitToVisualViewport);
      window.visualViewport?.removeEventListener("scroll", fitToVisualViewport);
      menu.dispose();
      root = null;
    },
    view(vnode) {
      const { store, onPagesHostCreated } = vnode.attrs;
      const state = store.getState();
      const shownWindow = shownWindowOf(state);
      const shownApp = shownWindow === null ? undefined : appByName(state, shownWindow.app);
      const isHome = state.phone.shown === null || state.phone.shown.kind === "home";
      const thresholdPx = store.getMetrics().dragThreshold;
      const menuRows =
        openMenu === null
          ? null
          : openMenu.kind === "window"
            ? rowsOfWindowMenu(store, openMenu.windowId)
            : rowsOfAppMenu(store, openMenu.app);
      return m(
        "div",
        {
          "data-phone-layout": "",
          class:
            "app-layout phone-layout fixed inset-x-0 top-0 flex h-dvh flex-col overflow-hidden bg-page " +
            "pt-[env(safe-area-inset-top)]",
        },
        [
          m(UpdateStalenessBanner),
          m(UpdateNoticeBanner, { store }),
          m(
            "div",
            {
              "data-phone-page-host": shownWindow?.id ?? "",
              class: "phone-page-host relative min-h-0 flex-1 overflow-hidden",
            },
            [
              m("div", {
                class: "live-pages pointer-events-none absolute inset-0 outline-none [&>*]:pointer-events-auto",
                oncreate: (created: m.VnodeDOM) => onPagesHostCreated(created.dom as HTMLElement),
                onbeforeupdate: () => false,
              }),
              shownWindow !== null && isAppShownStopped(state, shownApp)
                ? m("div", { class: "absolute inset-0" }, stoppedPlaceholder(shownApp))
                : null,
              isHome
                ? m(HomeGrid, {
                    apps: homeGridApps(state),
                    desktop: state.desktops[0] ?? null,
                    isStopped: (app) => isAppShownStopped(state, app),
                    onTap: (app) => void store.runHomeTile(app.name),
                    onLongPress: (app, target) => openMenuAt({ kind: "app", app: app.name }, target),
                    thresholdPx,
                  })
                : null,
            ],
          ),
          m(PhoneBar, {
            pill: phonePillOf(state),
            count: openWindowCount(state),
            avatar: state.avatar,
            onHome: () => store.goHome(),
            onPill: () => {
              windowQuery = "";
              store.openPhoneSheet("windows");
            },
            onPillLongPress: (target) => {
              if (shownWindow !== null) openMenuAt({ kind: "window", windowId: shownWindow.id }, target);
            },
            onNew: () => {
              startQuery = "";
              startHighlight = null;
              store.openPhoneSheet("start");
            },
            thresholdPx,
          }),
          m(Toasts, {
            toasts: store.toasts.current(),
            bottomClass:
              "bottom-[calc(var(--desk-phone-bar-height)+var(--desk-phone-safe-bottom)+var(--desk-toast-gap))]",
          }),
          sheetView(store),
          menuRows === null ? null : menu.view(menuRows),
        ],
      );
    },
  };
}

/** The pages the phone keeps mounted: the shown window's, and the pinned chat window's (where a phone lands). */
export function phoneMountPolicy(store: DesktopStore): {
  readonly kind: "shown";
  readonly windowId: string | null;
  readonly alsoKeep: readonly string[];
} {
  const state = store.getState();
  const pinned = pinnedChatWindowOf(state);
  return { kind: "shown", windowId: shownWindowOf(state)?.id ?? null, alsoKeep: pinned === null ? [] : [pinned.id] };
}
