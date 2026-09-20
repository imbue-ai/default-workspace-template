/**
 * The Running apps tray widget (concepts.md section 2.8): one icon per running, non-internal
 * app; a click opens a popover listing that app's windows on this desktop and its launch paths,
 * with "Add to desktop" for each launch path.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { menuDividerClass, menuRowClass } from "@imbue/workspace-ui/src/components/menu";
import type { AppRecord, LaunchPath, WindowRecord } from "../model/records";
import { shortcutKey } from "../model/records";
import { appGlyph, glyph } from "./glyphs";

const TRAY_GLYPH_SIZE = 16;
const ROW_GLYPH_SIZE = 14;

export interface RunningAppsWidgetAttrs {
  readonly apps: readonly AppRecord[];
  readonly openAppName: string | null;
  readonly onOpenApp: (app: AppRecord, event: MouseEvent) => void;
}

export const RunningAppsWidget: m.Component<RunningAppsWidgetAttrs> = {
  view(vnode) {
    const { apps, openAppName, onOpenApp } = vnode.attrs;
    return m(
      "div",
      { "data-tray-widget": "running-apps", class: "tray-running-apps flex items-center gap-0.5" },
      apps
        .filter((app) => app.is_running && !app.internal)
        .map((app) =>
          m(
            Button,
            {
              key: app.name,
              variant: "ghost",
              icon: true,
              sm: true,
              selected: openAppName === app.name,
              extra: "running-app min-h-(--desk-touch-target) min-w-(--desk-touch-target)",
              "data-running-app": app.name,
              "aria-label": app.display_name,
              "aria-haspopup": "dialog",
              "aria-expanded": openAppName === app.name ? "true" : "false",
              ...hoverTooltipAttrs(app.display_name),
              onclick: (event: MouseEvent) => onOpenApp(app, event),
            },
            m.trust(appGlyph(app, TRAY_GLYPH_SIZE)),
          ),
        ),
    );
  },
};

export interface RunningAppPopoverAttrs {
  readonly app: AppRecord;
  /** The app's windows on the active desktop, with the title each shows and whether it is minimized. */
  readonly windows: readonly { window: WindowRecord; title: string; isMinimized: boolean }[];
  /** The ``<app>:<launch>`` keys of the shortcuts already on the active desktop. */
  readonly desktopShortcutKeys: ReadonlySet<string>;
  readonly onPickWindow: (windowId: string) => void;
  readonly onRunLaunch: (launchPath: LaunchPath) => void;
  readonly onAddShortcut: (launchPath: LaunchPath) => void;
}

/** The popover's content: the app's windows here, then its launch paths. */
export const RunningAppPopover: m.Component<RunningAppPopoverAttrs> = {
  view(vnode) {
    const { app, windows, desktopShortcutKeys, onPickWindow, onRunLaunch, onAddShortcut } = vnode.attrs;
    return m("div", { "data-running-app-popover": app.name, class: "min-w-64" }, [
      m("div", { class: "type-section px-3 py-1 text-faint" }, app.display_name),
      windows.length === 0
        ? m("div", { class: "px-3 py-1 text-(length:--font-size-row) text-faint" }, "No windows on this desktop")
        : windows.map(({ window, title, isMinimized }) =>
            m(
              "button",
              {
                key: window.id,
                type: "button",
                "data-popover-window": window.id,
                class: `${menuRowClass()} ${isMinimized ? "text-faint" : "text-primary"}`,
                onclick: () => onPickWindow(window.id),
              },
              [
                m("span", { class: "min-w-0 flex-1 truncate" }, title),
                isMinimized ? m("span", { class: "type-helper text-faint" }, "minimized") : null,
              ],
            ),
          ),
      m("div", { class: menuDividerClass() }),
      app.launch_paths.map((launchPath) => {
        const key = shortcutKey(app.name, launchPath.id);
        const isOnDesktop = desktopShortcutKeys.has(key);
        return m("div", { key: launchPath.id, class: "flex items-center gap-1 px-3 py-0.5" }, [
          m(
            "button",
            {
              type: "button",
              "data-popover-launch": key,
              class:
                "flex min-w-0 flex-1 cursor-pointer items-center gap-2 rounded-md py-1 text-left text-(length:--font-size-row) text-primary hover:bg-fill-hover",
              onclick: () => onRunLaunch(launchPath),
            },
            [
              m("span", { class: "flex shrink-0 items-center text-faint" }, m.trust(glyph("plus", ROW_GLYPH_SIZE))),
              m("span", { class: "truncate" }, launchPath.label),
            ],
          ),
          m(
            Button,
            {
              variant: "ghost",
              sm: true,
              extra: "add-shortcut shrink-0",
              "data-add-shortcut": key,
              disabled: isOnDesktop,
              ...hoverTooltipAttrs(
                isOnDesktop
                  ? `${launchPath.label} is on this desktop already`
                  : `Add ${launchPath.label} to this desktop`,
              ),
              onclick: () => onAddShortcut(launchPath),
            },
            isOnDesktop ? "On desktop" : "Add to desktop",
          ),
        ]);
      }),
    ]);
  },
};
