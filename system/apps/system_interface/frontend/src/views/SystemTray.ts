/**
 * The system tray at the taskbar's right end: a row of self-contained tray widgets. V1 ships
 * Presence (who is connected, drawn only when someone is recorded) and Desktops; adding another
 * is adding a component here.
 */

import m from "mithril";
import type { Desktop } from "../model/records";
import { DesktopsWidget } from "./DesktopsWidget";
import { PresenceStrip } from "./PresenceStrip";

export interface SystemTrayAttrs {
  readonly desktops: readonly Desktop[];
  readonly activeDesktopId: string | null;
  readonly isDesktopsMenuOpen: boolean;
  readonly onSwitchDesktop: (desktopId: string) => void;
  readonly onOpenDesktopsMenu: (event: MouseEvent) => void;
  readonly onDesktopContextMenu: (desktopId: string, x: number, y: number, target: Element) => void;
}

export const SystemTray: m.Component<SystemTrayAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m("div", { "data-system-tray": "", class: "system-tray flex shrink-0 items-center gap-2 pl-2" }, [
      m(PresenceStrip),
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
