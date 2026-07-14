/**
 * Mobile replacement for the dockview tab strip.
 *
 * On small screens (see isMobileViewport) the dockview header is hidden and
 * this bar renders above the workspace instead: the active tab's title with a
 * tab-count badge on the left, and a "+" button on the right. Tapping the
 * title opens a bottom sheet listing every open tab (tap to switch, with
 * close/destroy actions); tapping "+" opens a bottom sheet with the same
 * items as the desktop "+" dropdown. Bottom sheets are the mobile idiom for
 * these menus: full-width, thumb-reachable, and scrollable when long.
 *
 * The component is pure presentation -- panel state, actions, and menu items
 * are supplied by DockviewWorkspace, which stays the single owner of dockview
 * bookkeeping.
 */

import m from "mithril";

export interface MobileTabInfo {
  panelId: string;
  title: string;
  isActive: boolean;
  // What the row's trash action destroys: an mngr agent, a tmux session, or
  // nothing (plain close-only tabs, and the primary agent which must never
  // be destroyed).
  destroyKind: "agent" | "terminal" | null;
}

export interface MobileAddMenuItem {
  label: string;
  action: () => void;
  dividerAfter?: boolean;
  disabled?: boolean;
  disabledReason?: string;
}

export interface MobileTabBarAttrs {
  tabs: MobileTabInfo[];
  // Built lazily on each redraw while the add sheet is open, so fleet
  // refreshes (browsers, terminals) show up as soon as they land.
  buildAddItems: () => MobileAddMenuItem[];
  // Fired when the add sheet opens; the owner kicks off its async fleet
  // refreshes here (mirroring the desktop dropdown's open handler).
  onAddSheetOpen: () => void;
  onSelectTab: (panelId: string) => void;
  onCloseTab: (panelId: string) => void;
  onDestroyTab: (panelId: string) => void;
}

// Media query mirroring the CSS breakpoint in responsive.css. Kept in one
// place so the JS-rendered bar and the CSS that hides the dockview header can
// never disagree.
const MOBILE_VIEWPORT_QUERY = "(max-width: 768px)";

let mobileQuery: MediaQueryList | null = null;

/** Whether the viewport is phone-sized. Subscribes mithril to breakpoint
 *  crossings on first use so the bar mounts/unmounts on resize. */
export function isMobileViewport(): boolean {
  if (mobileQuery === null) {
    mobileQuery = window.matchMedia(MOBILE_VIEWPORT_QUERY);
    mobileQuery.addEventListener("change", () => m.redraw());
  }
  return mobileQuery.matches;
}

const CHEVRON_DOWN_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<polyline points="6 9 12 15 18 9"/></svg>';

const CLOSE_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>';

const TRASH_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';

type SheetKind = "tabs" | "add";

export function MobileTabBar(): m.Component<MobileTabBarAttrs> {
  let openSheet: SheetKind | null = null;

  function closeSheet(): void {
    openSheet = null;
  }

  function renderSheet(title: string, rows: m.Children): m.Children {
    return [
      m("div", { class: "mobile-sheet-backdrop", onclick: closeSheet }),
      m("div", { class: "mobile-sheet" }, [
        m("div", { class: "mobile-sheet-grabber" }),
        m("div", { class: "mobile-sheet-title" }, title),
        m("div", { class: "mobile-sheet-rows" }, rows),
      ]),
    ];
  }

  function renderTabsSheet(attrs: MobileTabBarAttrs): m.Children {
    const rows =
      attrs.tabs.length === 0
        ? [m("div", { class: "mobile-sheet-empty" }, "No tabs open")]
        : attrs.tabs.map((tab) =>
            m(
              "div",
              {
                key: tab.panelId,
                class: tab.isActive ? "mobile-sheet-row mobile-sheet-row--active" : "mobile-sheet-row",
                onclick: () => {
                  closeSheet();
                  attrs.onSelectTab(tab.panelId);
                },
              },
              [
                m("span", { class: "mobile-sheet-row-label" }, tab.title),
                tab.destroyKind !== null
                  ? m(
                      "button",
                      {
                        type: "button",
                        class: "mobile-sheet-row-action mobile-sheet-row-action--destructive",
                        title: tab.destroyKind === "agent" ? "Destroy agent" : "Destroy terminal",
                        "aria-label": tab.destroyKind === "agent" ? "Destroy agent" : "Destroy terminal",
                        onclick: (event: MouseEvent) => {
                          event.stopPropagation();
                          closeSheet();
                          attrs.onDestroyTab(tab.panelId);
                        },
                      },
                      m.trust(TRASH_SVG),
                    )
                  : null,
                m(
                  "button",
                  {
                    type: "button",
                    class: "mobile-sheet-row-action",
                    title: "Close tab",
                    "aria-label": "Close tab",
                    onclick: (event: MouseEvent) => {
                      event.stopPropagation();
                      attrs.onCloseTab(tab.panelId);
                    },
                  },
                  m.trust(CLOSE_SVG),
                ),
              ],
            ),
          );
    return renderSheet("Tabs", rows);
  }

  function renderAddSheet(attrs: MobileTabBarAttrs): m.Children {
    const items = attrs.buildAddItems();
    const rows: m.Children[] = [];
    for (const item of items) {
      rows.push(
        m(
          "div",
          {
            class: item.disabled ? "mobile-sheet-row mobile-sheet-row--disabled" : "mobile-sheet-row",
            onclick: () => {
              if (item.disabled) {
                if (item.disabledReason) alert(item.disabledReason);
                return;
              }
              closeSheet();
              item.action();
            },
          },
          m("span", { class: "mobile-sheet-row-label" }, item.label),
        ),
      );
      if (item.dividerAfter) {
        rows.push(m("div", { class: "mobile-sheet-divider" }));
      }
    }
    return renderSheet("Open new", rows);
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const active = attrs.tabs.find((tab) => tab.isActive);
      return m("div", { class: "mobile-tab-bar-root" }, [
        m("div", { class: "mobile-tab-bar" }, [
          m(
            "button",
            {
              type: "button",
              class: "mobile-tab-bar-switcher",
              onclick: () => {
                openSheet = openSheet === "tabs" ? null : "tabs";
              },
            },
            [
              m("span", { class: "mobile-tab-bar-title" }, active?.title ?? "No tabs open"),
              attrs.tabs.length > 0 ? m("span", { class: "mobile-tab-bar-count" }, String(attrs.tabs.length)) : null,
              m("span", { class: "mobile-tab-bar-chevron" }, m.trust(CHEVRON_DOWN_SVG)),
            ],
          ),
          m(
            "button",
            {
              type: "button",
              class: "mobile-tab-bar-add",
              title: "Add tab",
              "aria-label": "Add tab",
              onclick: () => {
                if (openSheet === "add") {
                  openSheet = null;
                  return;
                }
                openSheet = "add";
                attrs.onAddSheetOpen();
              },
            },
            "+",
          ),
        ]),
        openSheet === "tabs" ? renderTabsSheet(attrs) : null,
        openSheet === "add" ? renderAddSheet(attrs) : null,
      ]);
    },
  };
}
