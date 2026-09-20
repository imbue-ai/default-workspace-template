/**
 * The system tray at the taskbar's right end: a row of self-contained tray widgets, each with
 * one popover. V1 ships Desktops and Running apps; adding a third is adding a component here.
 */

import m from "mithril";
import type { AppRecord, Desktop } from "../model/records";
import { DesktopsWidget } from "./DesktopsWidget";
import { RunningAppsWidget } from "./RunningAppsWidget";

export interface SystemTrayAttrs {
  readonly desktops: readonly Desktop[];
  readonly activeDesktopId: string | null;
  readonly apps: readonly AppRecord[];
  readonly isDesktopsMenuOpen: boolean;
  readonly openRunningAppName: string | null;
  readonly onSwitchDesktop: (desktopId: string) => void;
  readonly onOpenDesktopsMenu: (event: MouseEvent) => void;
  readonly onDesktopContextMenu: (desktopId: string, x: number, y: number) => void;
  readonly onOpenRunningApp: (app: AppRecord, event: MouseEvent) => void;
}

export const SystemTray: m.Component<SystemTrayAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m("div", { "data-system-tray": "", class: "system-tray flex shrink-0 items-center gap-2 pl-2" }, [
      m(RunningAppsWidget, {
        apps: attrs.apps,
        openAppName: attrs.openRunningAppName,
        onOpenApp: attrs.onOpenRunningApp,
      }),
      m("span", { class: "h-5 border-l border-default" }),
      m(DesktopsWidget, {
        desktops: attrs.desktops,
        activeDesktopId: attrs.activeDesktopId,
        isMenuOpen: attrs.isDesktopsMenuOpen,
        onSwitch: attrs.onSwitchDesktop,
        onOpenMenu: attrs.onOpenDesktopsMenu,
        onDesktopContextMenu: attrs.onDesktopContextMenu,
      }),
    ]);
  },
};
