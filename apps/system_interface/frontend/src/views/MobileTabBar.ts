/**
 * Mobile replacement for the dockview tab strip.
 *
 * On small screens (see isMobileViewport) the dockview header is hidden and
 * this bar renders above the workspace instead: a hamburger button at the top
 * left and the active tab's title beside it. The hamburger opens a single
 * bottom-sheet menu.
 *
 * The menu is one flat list of destinations -- everything the workspace can
 * show (agent chats, terminal sessions, browsers, apps), not just what has an
 * open panel. Tapping a row goes there (focusing the existing panel or
 * creating one); "open" is not a user-facing category on a single-pane
 * screen. Rows that DO have a loaded panel carry a close X (and a destroy
 * action where the underlying entity supports it) -- the X doubles as the
 * "currently loaded" signal. Below the destinations sit the same creation and
 * layout actions as the desktop "+" dropdown.
 *
 * The component is pure presentation -- rows, actions, and menu items are
 * supplied by DockviewWorkspace, which stays the single owner of dockview
 * bookkeeping.
 */

import m from "mithril";

export interface MobileMenuRow {
  // Stable identity for keyed list diffing: the panel id when open, else the
  // destination's ref-like identity.
  key: string;
  label: string;
  isActive: boolean;
  onSelect: () => void;
  // Present only when a dockview panel is currently loaded for this row; its
  // presence is what renders the close X.
  onClose?: () => void;
  // Present only for loaded rows whose entity can be destroyed (an mngr
  // agent, a tmux session). ``destroyLabel`` names the action for the
  // button's tooltip/aria-label.
  onDestroy?: () => void;
  destroyLabel?: string;
}

export interface MobileAddMenuItem {
  label: string;
  action: () => void;
  dividerAfter?: boolean;
  disabled?: boolean;
  disabledReason?: string;
}

export interface MobileTabBarAttrs {
  rows: MobileMenuRow[];
  // Built lazily on each redraw while the menu is open, so fleet refreshes
  // (browsers, terminals) show up as soon as they land.
  buildActionItems: () => MobileAddMenuItem[];
  // Fired when the menu opens; the owner kicks off its async fleet refreshes
  // here (mirroring the desktop dropdown's open handler).
  onMenuOpen: () => void;
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

const HAMBURGER_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/></svg>';

const CLOSE_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>';

const TRASH_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';

export function MobileTabBar(): m.Component<MobileTabBarAttrs> {
  let menuOpen = false;

  function closeMenu(): void {
    menuOpen = false;
  }

  function renderDestinationRows(rows: MobileMenuRow[]): m.Children[] {
    if (rows.length === 0) {
      return [m("div", { class: "mobile-sheet-empty" }, "Nothing to open yet")];
    }
    return rows.map((row) =>
      m(
        "div",
        {
          key: row.key,
          class: row.isActive ? "mobile-sheet-row mobile-sheet-row--active" : "mobile-sheet-row",
          onclick: () => {
            closeMenu();
            row.onSelect();
          },
        },
        [
          m("span", { class: "mobile-sheet-row-label" }, row.label),
          row.onDestroy !== undefined
            ? m(
                "button",
                {
                  type: "button",
                  class: "mobile-sheet-row-action mobile-sheet-row-action--destructive",
                  title: row.destroyLabel ?? "Destroy",
                  "aria-label": row.destroyLabel ?? "Destroy",
                  onclick: (event: MouseEvent) => {
                    event.stopPropagation();
                    closeMenu();
                    row.onDestroy?.();
                  },
                },
                m.trust(TRASH_SVG),
              )
            : null,
          row.onClose !== undefined
            ? m(
                "button",
                {
                  type: "button",
                  class: "mobile-sheet-row-action",
                  title: "Close tab",
                  "aria-label": "Close tab",
                  onclick: (event: MouseEvent) => {
                    event.stopPropagation();
                    row.onClose?.();
                  },
                },
                m.trust(CLOSE_SVG),
              )
            : null,
        ],
      ),
    );
  }

  function renderActionRows(attrs: MobileTabBarAttrs): m.Children[] {
    const items = attrs.buildActionItems();
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
              closeMenu();
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
    return rows;
  }

  function renderMenuSheet(attrs: MobileTabBarAttrs): m.Children {
    return [
      m("div", { class: "mobile-sheet-backdrop", onclick: closeMenu }),
      m("div", { class: "mobile-sheet" }, [
        m("div", { class: "mobile-sheet-grabber" }),
        m("div", { class: "mobile-sheet-rows" }, [
          // The destination rows stay a nested array: mithril normalizes it
          // into its own fragment, which keeps the keyed rows uniformly keyed
          // among themselves without keying these section siblings.
          renderDestinationRows(attrs.rows),
          m("div", { class: "mobile-sheet-divider" }),
          ...renderActionRows(attrs),
        ]),
      ]),
    ];
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const active = attrs.rows.find((row) => row.isActive);
      return m("div", { class: "mobile-tab-bar-root" }, [
        m("div", { class: "mobile-tab-bar" }, [
          m(
            "button",
            {
              type: "button",
              class: "mobile-tab-bar-menu-button",
              title: "Menu",
              "aria-label": "Menu",
              onclick: () => {
                menuOpen = !menuOpen;
                if (menuOpen) {
                  attrs.onMenuOpen();
                }
              },
            },
            m.trust(HAMBURGER_SVG),
          ),
          m("span", { class: "mobile-tab-bar-title" }, active?.label ?? "No tabs open"),
        ]),
        menuOpen ? renderMenuSheet(attrs) : null,
      ]);
    },
  };
}
