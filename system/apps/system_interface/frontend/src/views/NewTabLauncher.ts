/**
 * The New Tab launcher: a full-page panel answering one question -- what do you want in this
 * pane -- in three parts: "Open new" runs an app's action, "In this project" jumps to an
 * instance the active view already shows, and "On this machine" reaches everything else.
 *
 * Opening a row from "On this machine" files it into the active project (the workspace does
 * that, as it owns every open). Everything is the unfiltered view, so it has no tab set to
 * split against and renders the single machine-wide table instead.
 *
 * Nothing here knows what any app is: the tiles are the apps' own primary actions, the rows
 * carry the apps' own names and icons, and the kind filter is by app. The list building,
 * filtering and recency ordering are exported as pure functions so they can be tested
 * without a DOM.
 */

import m from "mithril";
import { CHAT_APP_NAME, CHAT_NEW_ACTION } from "../models/chatApp";
import type { AppAction, AppRecord, InstanceStatus } from "../models/Inventory";
import { serviceIconMarkup } from "./appIcon";
import { getAccounts, getSelectedAccount, openProviderChooser, selectAccount } from "../models/Providers";
import { Portal } from "./portal";
import { accountRow, emptyAccountRowState } from "./accountRow";
import * as css from "./modelCardStyles";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon } from "./icons";

/** One "Open new" tile: an app and the action it runs. */
export interface LaunchTile {
  app: AppRecord;
  action: AppAction;
}

/** One instance the launcher can open. */
export interface LauncherRow {
  address: string;
  appName: string;
  appDisplayName: string;
  label: string;
  status: InstanceStatus;
  // Epoch milliseconds of the instance's last activity, or null when its app reports none.
  lastActiveMs: number | null;
}

export type LauncherSectionKey = "in-project" | "on-machine";

export interface LauncherSection {
  key: LauncherSectionKey;
  title: string;
  rows: LauncherRow[];
}

const OPEN_NEW_TITLE = "Open new";
const IN_PROJECT_TITLE = "In this project";
const ON_MACHINE_TITLE = "On this machine";

const SECTION_HEADING_CLASS = "text-text-faint text-[11px] font-semibold tracking-wider uppercase";

// The chat app's ``new`` action takes the provider account the picker beside its tile chose.
// CLEANUP: phase 10 of the workspace app model moves the picker into the chat app's own page.

/**
 * Assemble the launcher's tables. A project's "In this project" table IS its tab set, in tab
 * order; "On this machine" is the rest of the machine, deduped by address. Everything gets the
 * single machine-wide table.
 */
export function buildLauncherSections(
  machineRows: readonly LauncherRow[],
  memberRows: readonly LauncherRow[],
  isEverything: boolean,
): LauncherSection[] {
  if (isEverything) {
    return [{ key: "on-machine", title: ON_MACHINE_TITLE, rows: [...machineRows] }];
  }
  const members = new Set(memberRows.map((row) => row.address));
  return [
    { key: "in-project", title: IN_PROJECT_TITLE, rows: [...memberRows] },
    { key: "on-machine", title: ON_MACHINE_TITLE, rows: machineRows.filter((row) => !members.has(row.address)) },
  ];
}

/** Drop the rows whose app the user unchecked in this table's filter. The state is the set of
 *  HIDDEN apps, so an app that appears later starts visible. */
export function filterRowsByApp(rows: readonly LauncherRow[], hiddenApps: ReadonlySet<string>): LauncherRow[] {
  return rows.filter((row) => !hiddenApps.has(row.appName));
}

/** Order rows most-recently-active first; rows with no known recency go last, ties keep order. */
export function sortRowsByRecency(rows: readonly LauncherRow[]): LauncherRow[] {
  return [...rows].sort((left, right) => {
    if (left.lastActiveMs === right.lastActiveMs) return 0;
    if (left.lastActiveMs === null) return 1;
    if (right.lastActiveMs === null) return -1;
    return right.lastActiveMs - left.lastActiveMs;
  });
}

/** The apps present in a table, in first-seen order, with the name each is displayed under. */
export function appsInRows(rows: readonly LauncherRow[]): { name: string; displayName: string }[] {
  const seen = new Map<string, string>();
  for (const row of rows) {
    if (!seen.has(row.appName)) seen.set(row.appName, row.appDisplayName);
  }
  return Array.from(seen, ([name, displayName]) => ({ name, displayName }));
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const WEEK_MS = 7 * DAY_MS;

/** The recency column's text: coarse and relative. A future timestamp reads as "just now". */
export function formatRecency(lastActiveMs: number | null, nowMs: number): string {
  if (lastActiveMs === null) return "—";
  const age = nowMs - lastActiveMs;
  if (age < MINUTE_MS) return "just now";
  if (age < HOUR_MS) return `${Math.floor(age / MINUTE_MS)}m ago`;
  if (age < DAY_MS) return `${Math.floor(age / HOUR_MS)}h ago`;
  if (age < WEEK_MS) return `${Math.floor(age / DAY_MS)}d ago`;
  if (age < 2 * WEEK_MS) return "last week";
  return `${Math.floor(age / WEEK_MS)}w ago`;
}

const XMLNS = "http://www.w3.org/2000/svg";

const LAUNCHER_PATHS = {
  app: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>',
  filter:
    '<line x1="4" y1="7" x2="20" y2="7"/><line x1="7" y1="12" x2="17" y2="12"/><line x1="10" y1="17" x2="14" y2="17"/>',
} as const;

function launcherIcon(glyph: keyof typeof LAUNCHER_PATHS, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${LAUNCHER_PATHS[glyph]}</svg>`
  );
}

const GLYPH_SIZE = 15;

/** The glyph one row (or tile) wears: the app's own icon, or its monogram. */
function appGlyph(appName: string): string {
  return serviceIconMarkup(appName, GLYPH_SIZE, launcherIcon("app", GLYPH_SIZE));
}

const STARTING_TITLE = "Starting…";

export interface NewTabLauncherAttrs {
  tiles: readonly LaunchTile[];
  // Everything the machine holds.
  rows: readonly LauncherRow[];
  // The active project's tab set as rows, in tab order. Ignored when isEverything is set.
  memberRows: readonly LauncherRow[];
  isEverything: boolean;
  nowMs?: number;
  // Whether this pane is waiting on an action it already ran: the tiles stand down and say so.
  isAwaitingCreate?: boolean;
  // Run an app's action in this pane.
  onRunAction: (app: AppRecord, actionId: string, params: Record<string, string>) => void;
  // Open an instance into this pane (the workspace files it into the project when it is not there yet).
  onOpenRow: (row: LauncherRow) => void;
}

/** The provider half of the chat tile: WHICH new chat the button starts. */
function ProviderPicker(): m.Component<{ onSignedIn: (accountId: string) => void }> {
  let open = false;
  let anchor: DOMRect | null = null;
  const rowState = emptyAccountRowState();

  function resetRows(): void {
    rowState.confirmingRemoval = null;
    rowState.renamingId = null;
    rowState.renameDraft = "";
  }

  function close(): void {
    open = false;
    anchor = null;
    resetRows();
  }

  function handleOutsideMousedown(event: MouseEvent): void {
    if (!open) return;
    if ((event.target as Element | null)?.closest?.(`[${PICKER_ATTR}]`) != null) return;
    close();
    m.redraw();
  }

  function placement(rect: DOMRect): string {
    const margin = 8;
    const left = Math.min(Math.max(rect.left, margin), Math.max(margin, window.innerWidth - margin - PICKER_WIDTH));
    const below = window.innerHeight - rect.bottom - margin;
    const vertical =
      below >= PICKER_MIN_HEIGHT
        ? `top: ${rect.bottom + 4}px; max-height: ${below - 4}px;`
        : `bottom: ${window.innerHeight - rect.top + 4}px; max-height: ${Math.max(0, rect.top - margin - 4)}px;`;
    return `left: ${left}px; ${vertical} width: ${PICKER_WIDTH}px;`;
  }

  return {
    oninit() {
      document.addEventListener("mousedown", handleOutsideMousedown);
    },
    onremove() {
      document.removeEventListener("mousedown", handleOutsideMousedown);
    },
    view(vnode) {
      const selected = getSelectedAccount();
      const accounts = getAccounts();
      const trigger = m(
        "button",
        {
          type: "button",
          class:
            "text-text-secondary hover:bg-bg-hover hover:text-text-primary flex min-w-0 max-w-[190px] " +
            "cursor-pointer items-center gap-1 truncate bg-transparent py-0 pr-2 pl-3 text-[13px] focus:outline-none",
          "aria-label": "Provider for the new chat",
          "aria-expanded": open ? "true" : "false",
          [PICKER_ATTR]: "trigger",
          onclick: (event: MouseEvent) => {
            event.stopPropagation();
            if (open) {
              close();
              return;
            }
            open = true;
            anchor = (event.currentTarget as HTMLElement).getBoundingClientRect();
            resetRows();
          },
        },
        [
          m("span", { class: "min-w-0 truncate" }, selected?.label ?? "No provider yet"),
          m("span", { class: "shrink-0 text-text-faint" }, m.trust(icon("chevron-down", { size: 14 }))),
        ],
      );

      if (!open || anchor === null) return trigger;

      const menu = m(
        "div",
        {
          class: css.FLYOUT,
          [PICKER_ATTR]: "menu",
          style: placement(anchor),
        },
        [
          m(
            "div",
            { class: css.FLYOUT_SCROLL },
            accounts.length === 0
              ? [m("div", { class: css.FLYOUT_EMPTY }, "No providers yet.")]
              : accounts.map((candidate) =>
                  accountRow({
                    row: candidate,
                    isCurrent: candidate.id === selected?.id,
                    rowClass: candidate.id === selected?.id ? css.ACCOUNT_ROW_SELECTED : css.ACCOUNT_ROW,
                    onSelect: () => {
                      selectAccount(candidate.id);
                      close();
                    },
                    state: rowState,
                  }),
                ),
          ),
          m(
            "button",
            {
              type: "button",
              class: css.FLYOUT_ADD,
              onclick: (event: MouseEvent) => {
                event.stopPropagation();
                close();
                openProviderChooser({ onSignedIn: (accountId) => vnode.attrs.onSignedIn(accountId) });
              },
            },
            "+ Add a provider",
          ),
        ],
      );

      return [trigger, m(Portal, { children: menu })];
    },
  };
}

const PICKER_ATTR = "data-provider-picker";
const PICKER_WIDTH = 260;
const PICKER_MIN_HEIGHT = 120;

// Marks a section's filter toggle, so the menu's outside-press listener leaves the toggle's
// own press to the click that follows it.
const FILTER_TOGGLE_ATTR = "data-launcher-filter-toggle";

export function NewTabLauncher(): m.Component<NewTabLauncherAttrs> {
  const hiddenAppsBySection: Record<LauncherSectionKey, Set<string>> = {
    "in-project": new Set(),
    "on-machine": new Set(),
  };
  let openFilterFor: LauncherSectionKey | null = null;
  let menuElement: HTMLElement | null = null;

  const closeFilterMenu = (): void => {
    openFilterFor = null;
    m.redraw();
  };

  const onDocumentPointerDown = (event: Event): void => {
    if (!(event.target instanceof Node)) return closeFilterMenu();
    if (menuElement !== null && menuElement.contains(event.target)) return;
    // A press on a toggle is the click that follows: it closes, or moves, the menu itself.
    if (event.target instanceof Element && event.target.closest(`[${FILTER_TOGGLE_ATTR}]`) !== null) return;
    closeFilterMenu();
  };

  const onDocumentKeyDown = (event: KeyboardEvent): void => {
    if (event.key === "Escape") closeFilterMenu();
  };

  function filterMenuRow(section: LauncherSection, app: { name: string; displayName: string }): m.Vnode {
    const hidden = hiddenAppsBySection[section.key];
    const isShown = !hidden.has(app.name);
    return m(
      "label",
      {
        key: app.name,
        class: "flex h-8 cursor-pointer items-center gap-2 px-3 text-[13px] text-text-primary hover:bg-bg-hover",
      },
      [
        m("span", { class: "relative flex h-4 w-4 shrink-0 items-center justify-center" }, [
          m("input", {
            type: "checkbox",
            checked: isShown,
            onchange: () => {
              if (hidden.has(app.name)) {
                hidden.delete(app.name);
              } else {
                hidden.add(app.name);
              }
            },
            class:
              "absolute inset-0 m-0 h-4 w-4 cursor-pointer appearance-none rounded border " +
              (isShown ? "border-accent bg-accent" : "border-border bg-surface"),
          }),
          isShown
            ? m(
                "span",
                { class: "pointer-events-none relative text-white" },
                m.trust(icon("check", { size: 11, strokeWidth: 3 })),
              )
            : null,
        ]),
        m(
          "span",
          { class: "text-text-faint flex w-5 shrink-0 items-center justify-center" },
          m.trust(appGlyph(app.name)),
        ),
        app.displayName,
      ],
    );
  }

  function filterMenu(section: LauncherSection): m.Vnode {
    const hidden = hiddenAppsBySection[section.key];
    const isPristine = hidden.size === 0;
    return m(
      "div",
      {
        class:
          "absolute top-full right-0 z-30 mt-1 min-w-[170px] rounded-lg border border-border bg-surface py-1 shadow-lg",
        oncreate: (vnode: m.VnodeDOM) => {
          menuElement = vnode.dom as HTMLElement;
          document.addEventListener("pointerdown", onDocumentPointerDown);
          document.addEventListener("keydown", onDocumentKeyDown);
        },
        onremove: () => {
          menuElement = null;
          document.removeEventListener("pointerdown", onDocumentPointerDown);
          document.removeEventListener("keydown", onDocumentKeyDown);
        },
      },
      [
        appsInRows(section.rows).map((app) => filterMenuRow(section, app)),
        m("div", { class: "my-1 border-t border-border" }),
        m(
          "button",
          {
            type: "button",
            disabled: isPristine,
            class:
              "flex h-8 w-full items-center px-3 text-left text-[13px] " +
              (isPristine ? "text-text-faint cursor-default" : "text-text-secondary cursor-pointer hover:bg-bg-hover"),
            onclick: () => hidden.clear(),
          },
          "Reset filters",
        ),
      ],
    );
  }

  function memberRow(row: LauncherRow, nowMs: number, onOpen: (row: LauncherRow) => void): m.Vnode {
    const isStopped = row.status === "stopped";
    return m(
      "button",
      {
        key: row.address,
        type: "button",
        "data-address": row.address,
        class:
          "new-tab-launcher-row flex h-9 w-full cursor-pointer items-center gap-3 rounded-md px-2 text-left " +
          "text-[13px] hover:bg-bg-hover " +
          (isStopped ? "new-tab-launcher-row-stopped text-text-faint opacity-60" : "text-text-primary"),
        onclick: () => onOpen(row),
      },
      [
        m(
          "span",
          { class: "text-text-faint flex w-5 shrink-0 items-center justify-center" },
          m.trust(appGlyph(row.appName)),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, row.label),
        m("span", { class: "text-text-faint w-24 shrink-0 truncate" }, row.appDisplayName),
        m(
          "span",
          { class: "text-text-faint w-28 shrink-0 truncate text-right" },
          formatRecency(row.lastActiveMs, nowMs),
        ),
      ],
    );
  }

  function sectionView(section: LauncherSection, attrs: NewTabLauncherAttrs, nowMs: number): m.Vnode {
    const visible = sortRowsByRecency(filterRowsByApp(section.rows, hiddenAppsBySection[section.key]));
    const nothingHere =
      section.key === "on-machine" ? "Nothing else is running on this machine." : "Nothing is in this project yet.";
    const emptyMessage = section.rows.length === 0 ? nothingHere : "No tabs match this filter.";

    return m("section", { key: section.key, class: "new-tab-launcher-section mt-6", "data-section": section.key }, [
      m("div", { class: "relative mb-1 flex h-6 items-center justify-between px-2" }, [
        m("h2", { class: SECTION_HEADING_CLASS }, section.title),
        m(
          "button",
          {
            type: "button",
            "aria-expanded": openFilterFor === section.key ? "true" : "false",
            [FILTER_TOGGLE_ATTR]: "",
            class:
              "text-text-faint flex h-6 w-6 cursor-pointer items-center justify-center rounded " +
              "hover:bg-bg-hover hover:text-text-primary",
            onclick: () => {
              openFilterFor = openFilterFor === section.key ? null : section.key;
            },
            ...hoverTooltipAttrs("Filter by app"),
          },
          m.trust(launcherIcon("filter", GLYPH_SIZE)),
        ),
        openFilterFor === section.key ? filterMenu(section) : null,
      ]),
      visible.length === 0
        ? m("p", { class: "text-text-faint px-2 py-1 text-[13px]" }, emptyMessage)
        : visible.map((row) => memberRow(row, nowMs, attrs.onOpenRow)),
    ]);
  }

  function tileView(tile: LaunchTile, attrs: NewTabLauncherAttrs): m.Vnode {
    const isChatTile = tile.app.name === CHAT_APP_NAME && tile.action.id === CHAT_NEW_ACTION;
    const isDisabled = attrs.isAwaitingCreate === true;
    const run = (params: Record<string, string> = {}): void => attrs.onRunAction(tile.app, tile.action.id, params);
    return m(
      "div",
      {
        key: `${tile.app.name}:${tile.action.id}`,
        class:
          "border-border flex h-9 items-stretch overflow-hidden rounded-lg border " +
          (isChatTile ? "min-w-0 flex-[1.7]" : "min-w-0 flex-1") +
          (isDisabled ? " text-text-faint" : " text-text-primary"),
      },
      [
        m(
          "button",
          {
            type: "button",
            "aria-disabled": isDisabled ? "true" : undefined,
            "data-launch": `${tile.app.name}:${tile.action.id}`,
            class:
              "new-tab-launcher-tile flex min-w-0 flex-1 items-center justify-center gap-2 px-4 " +
              "text-[13px] font-medium " +
              (isDisabled ? "cursor-not-allowed" : "hover:bg-bg-hover cursor-pointer"),
            onclick: isDisabled
              ? undefined
              : () => run(isChatTile ? { account_id: getSelectedAccount()?.id ?? "" } : {}),
            ...(isDisabled ? {} : hoverTooltipAttrs(tile.action.label)),
          },
          [
            m("span", { class: "text-text-faint flex shrink-0 items-center" }, m.trust(appGlyph(tile.app.name))),
            m("span", { class: "min-w-0 truncate" }, tile.app.display_name),
          ],
        ),
        isChatTile ? m("span", { class: "bg-border w-px self-stretch" }) : null,
        isChatTile ? m(ProviderPicker, { onSignedIn: (accountId) => run({ account_id: accountId }) }) : null,
      ],
    );
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const nowMs = attrs.nowMs ?? Date.now();
      const sections = buildLauncherSections(attrs.rows, attrs.memberRows, attrs.isEverything);

      return m("div", { class: "new-tab-launcher bg-surface h-full w-full overflow-y-auto px-6 py-5" }, [
        m("div", { class: "mx-auto w-full max-w-4xl" }, [
          m(
            "h2",
            { class: `${SECTION_HEADING_CLASS} mb-2 px-2` },
            attrs.isAwaitingCreate === true ? STARTING_TITLE : OPEN_NEW_TITLE,
          ),
          attrs.tiles.length === 0
            ? m("p", { class: "text-text-faint px-2 py-1 text-[13px]" }, "No apps are registered on this machine yet.")
            : m(
                "div",
                { class: "flex flex-wrap gap-2 px-2" },
                attrs.tiles.map((tile) => tileView(tile, attrs)),
              ),
          sections.map((section) => sectionView(section, attrs, nowMs)),
        ]),
      ]);
    },
  };
}
