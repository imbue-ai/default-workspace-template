/**
 * The New Tab launcher: a full-page panel answering two questions about this pane -- what do you
 * want in it, and what could you start. From the top: a search field; "Open new" (the apps'
 * primary actions as tiles, the built-in four leading in a fixed order, every other app on a
 * second row); "In this project" (the active view's tab set, omitted when empty); then, under a
 * dashed rule, the two offers of things to start: "Start something" (hardcoded intents, each a
 * chat seeded with a prompt) and "Start from a template" (the published catalog, by category, in
 * sideways rails). Typing in the search field swaps the sections for results: the machine's
 * instances and actions, the matching intents, the matching templates.
 *
 * Nothing here knows what any app is: the tables carry the apps' own names and icons, the kind
 * filter is by app, the leading tile row is the apps that declare a ``launcher_rank`` in their
 * manifests, and a seeded prompt goes to whichever app declares an action with a ``message`` param
 * (contracts.md sections 2 and 3). The list building, filtering, and ordering are exported as pure
 * functions so they can be tested without a DOM. The template cards and their rails are
 * ``TemplateShelves.ts``, and the card's detail dialog ``TemplateDetailModal.ts``.
 *
 * Every marker an e2e suite finds the page by is kept: ``.new-tab-launcher``,
 * ``.new-tab-launcher-tile[data-launch]``, ``.new-tab-launcher-row[data-address]``, and
 * ``.new-tab-launcher-section[data-section]``.
 */

import m from "mithril";
import type { AppAction, AppRecord, InstanceStatus } from "../models/Inventory";
import type { CatalogTemplate, TemplateCatalogState } from "../models/TemplateCatalog";
import { resolveShelves, searchTemplates } from "../models/TemplateCatalog";
import { matchesQuery } from "../models/search";
import { serviceIconMarkup } from "./components/appIcon";
import {
  START_OPTIONS,
  START_PAGE_SIZE,
  hasMoreStartOptions,
  nextStartCount,
  searchStartOptions,
  startGlyph,
  visibleStartOptions,
} from "./startSomething";
import type { StartOption } from "./startSomething";
import { TemplateDetailModal } from "./TemplateDetailModal";
import { TemplateCard, TemplateShelves } from "./TemplateShelves";
import { Button, buttonClass } from "@imbue/workspace-ui/src/components/Button";
import { menuCardClass, menuDividerClass, menuRowClass } from "@imbue/workspace-ui/src/components/menu";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";

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
const START_SOMETHING_TITLE = "Start something";
const TEMPLATES_TITLE = "Start from a template";
const SEARCH_TEMPLATES_TITLE = "Templates";
const SEE_MORE_LABEL = "See more";
const SEARCH_PLACEHOLDER = "Search apps, chats, and templates";
const TEMPLATES_LOADING_MESSAGE = "Loading templates…";
const TEMPLATES_FAILED_MESSAGE = "Failed to load templates.";
const NO_CHAT_APP_REASON = "No app on this machine can start a chat";
const TEMPLATES_NOT_OFFERED_REASON = "No template catalog is configured on this machine";

const SECTION_HEADING_CLASS = "type-section text-faint";

// The create param a seeded prompt rides: an action declaring it takes a first message
// (contracts.md section 2, the chat manifest row declares one on ``new``).
export const MESSAGE_PARAM = "message";

/** Adopt a template into this machine: the first message of the chat "Make it mine" starts. */
export function adoptTemplateMessage(template: CatalogTemplate): string {
  return `/use-template ${template.repository_url}`;
}

/** Have a new machine made from a template: the first message of the chat that action starts. */
export function createMachineFromTemplateMessage(template: CatalogTemplate): string {
  return (
    `Please create a new Minds machine for me from the template at ${template.repository_url} ` +
    "(the minds-api skill can create one). Walk me through anything it needs from me, like permissions " +
    "or accounts, and tell me when it is ready."
  );
}

export interface LaunchTileRows {
  /** The apps that declare a ``launcher_rank``, lowest first (registry order breaks a tie). */
  leading: LaunchTile[];
  /** Every other app, in registry order. */
  other: LaunchTile[];
}

/** The two tile rows: the ranked apps in rank order, then every app without a rank. */
export function splitLaunchTiles(tiles: readonly LaunchTile[]): LaunchTileRows {
  const ranked = tiles.filter((tile) => tile.app.launcher_rank !== null);
  const leading = [...ranked].sort((left, right) => (left.app.launcher_rank ?? 0) - (right.app.launcher_rank ?? 0));
  const other = tiles.filter((tile) => tile.app.launcher_rank === null);
  return { leading, other };
}

/** Where a seeded prompt goes: the first app (in tile order) with an action that takes a ``message``. */
export function promptTargetOfTiles(tiles: readonly LaunchTile[]): LaunchTile | null {
  for (const tile of tiles) {
    const action = tile.app.actions.find((candidate) => candidate.params.includes(MESSAGE_PARAM));
    if (action !== undefined) return { app: tile.app, action };
  }
  return null;
}

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

/**
 * The tables the RESTING page shows: the project's own tab set (or the whole machine under
 * Everything), and only when it holds something -- a brand-new project goes straight from
 * "Open new" to the offers. The rest of the machine is reached through search.
 */
export function restingSections(
  machineRows: readonly LauncherRow[],
  memberRows: readonly LauncherRow[],
  isEverything: boolean,
): LauncherSection[] {
  const [first] = buildLauncherSections(machineRows, memberRows, isEverything);
  return first.rows.length === 0 ? [] : [first];
}

/** The machine's instances a query finds: by title or by the app they belong to. */
export function searchLauncherRows(rows: readonly LauncherRow[], query: string): LauncherRow[] {
  return rows.filter((row) => matchesQuery(query, row.label, row.appDisplayName, row.appName));
}

/** What a tile reads as when a search restates it as a row: "Open new terminal". */
export function actionRowLabel(tile: LaunchTile): string {
  return `Open new ${tile.app.display_name.toLowerCase()}`;
}

/** The "Open new" tiles a query finds, as the actions a search can answer with: by the row text the
 *  match renders as, the app's names, or the action's own label. */
export function searchTiles(tiles: readonly LaunchTile[], query: string): LaunchTile[] {
  return tiles.filter((tile) =>
    matchesQuery(query, actionRowLabel(tile), tile.app.display_name, tile.app.name, tile.action.label),
  );
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
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
} as const;

function launcherIcon(glyph: keyof typeof LAUNCHER_PATHS, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${LAUNCHER_PATHS[glyph]}</svg>`
  );
}

const GLYPH_SIZE = 15;
const START_GLYPH_SIZE = 24;

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
  // The template catalog, as the page's shared fetch stands.
  catalog: TemplateCatalogState;
  nowMs?: number;
  // Whether this pane is waiting on an action it already ran: the tiles stand down and say so.
  isAwaitingCreate?: boolean;
  // Run an app's action in this pane, with the create's params (a seeded chat's message).
  onRunAction: (app: AppRecord, actionId: string, params: Readonly<Record<string, string>>) => void;
  // Open an instance into this pane (the workspace files it into the project when it is not there yet).
  onOpenRow: (row: LauncherRow) => void;
}

// Marks a section's filter toggle, so the menu's outside-press listener leaves the toggle's
// own press to the click that follows it.
const FILTER_TOGGLE_ATTR = "data-launcher-filter-toggle";

const ROW_CLASS =
  "new-tab-launcher-row flex h-9 w-full cursor-pointer items-center gap-3 rounded-md px-2 text-left " +
  "text-(length:--font-size-row) hover:bg-fill-hover ";

export function NewTabLauncher(): m.Component<NewTabLauncherAttrs> {
  const hiddenAppsBySection: Record<LauncherSectionKey, Set<string>> = {
    "in-project": new Set(),
    "on-machine": new Set(),
  };
  let openFilterFor: LauncherSectionKey | null = null;
  let menuElement: HTMLElement | null = null;
  let query = "";
  let startShownCount = START_PAGE_SIZE;
  let detailTemplate: CatalogTemplate | null = null;
  // Set by the "Start from a template" tile; the templates section scrolls itself into view on
  // its next create or update and clears it (from search, the section mounts only after the
  // click empties the query).
  let isScrollToTemplatesPending = false;

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

  function isSearching(): boolean {
    return query.trim() !== "";
  }

  /** Whether anything that starts a seeded chat (a prompt tile, the detail dialog's actions) stands
   *  down right now, and the reason a tooltip gives when there is one to give: no app on the
   *  machine takes a first message, or this pane is already waiting on a create (which the page
   *  says under "Open new", so it needs no tooltip). */
  function promptStartDisabling(attrs: NewTabLauncherAttrs): { isDisabled: boolean; reason: string | null } {
    if (promptTargetOfTiles(attrs.tiles) === null) return { isDisabled: true, reason: NO_CHAT_APP_REASON };
    return { isDisabled: attrs.isAwaitingCreate === true, reason: null };
  }

  function startChat(attrs: NewTabLauncherAttrs, message: string): void {
    const target = promptTargetOfTiles(attrs.tiles);
    if (target === null) return;
    attrs.onRunAction(target.app, target.action.id, { [MESSAGE_PARAM]: message });
  }

  // ---------- the search field ----------

  function searchField(): m.Vnode {
    return m(
      "div",
      {
        class:
          "new-tab-launcher-search group flex h-9 items-center gap-2 rounded-lg border border-default bg-surface " +
          "px-2.5 focus-within:border-accent",
      },
      [
        m("span", { class: "flex shrink-0 items-center text-faint" }, m.trust(icon("search", { size: 14 }))),
        m("input", {
          type: "text",
          "aria-label": SEARCH_PLACEHOLDER,
          placeholder: SEARCH_PLACEHOLDER,
          value: query,
          class:
            "min-w-0 flex-1 bg-transparent text-(length:--font-size-body) text-primary outline-none " +
            "placeholder:text-faint",
          oninput: (event: InputEvent) => {
            query = (event.target as HTMLInputElement).value;
          },
          onkeydown: (event: KeyboardEvent) => {
            if (event.key === "Escape") query = "";
          },
        }),
        query === ""
          ? null
          : m(
              Button,
              {
                variant: "ghost",
                icon: true,
                xs: true,
                "aria-label": "Clear search",
                extra: "new-tab-launcher-search-clear",
                onclick: () => {
                  query = "";
                },
              },
              m.trust(icon("close", { size: 14 })),
            ),
      ],
    );
  }

  // ---------- the tables ----------

  function filterMenuRow(section: LauncherSection, app: { name: string; displayName: string }): m.Vnode {
    const hidden = hiddenAppsBySection[section.key];
    const isShown = !hidden.has(app.name);
    return m(
      "label",
      {
        key: app.name,
        class: menuRowClass({ extra: "text-(length:--font-size-row) text-primary" }),
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
              (isShown ? "border-accent bg-accent" : "border-default bg-surface"),
          }),
          isShown
            ? m(
                "span",
                { class: "pointer-events-none relative text-white" },
                m.trust(icon("check", { size: 11, strokeWidth: 3 })),
              )
            : null,
        ]),
        m("span", { class: "text-faint flex w-5 shrink-0 items-center justify-center" }, m.trust(appGlyph(app.name))),
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
        class: menuCardClass("absolute top-full right-0 mt-1 min-w-[170px]"),
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
        m("div", { class: menuDividerClass() }),
        m(
          "button",
          {
            type: "button",
            disabled: isPristine,
            class:
              "flex h-8 w-full items-center px-3 text-left text-(length:--font-size-row) " +
              (isPristine ? "text-faint cursor-default" : "text-secondary cursor-pointer hover:bg-fill-hover"),
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
        class: ROW_CLASS + (isStopped ? "new-tab-launcher-row-stopped text-faint opacity-60" : "text-primary"),
        onclick: () => onOpen(row),
      },
      [
        m(
          "span",
          { class: "text-faint flex w-5 shrink-0 items-center justify-center" },
          m.trust(appGlyph(row.appName)),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, row.label),
        m("span", { class: "text-faint w-24 shrink-0 truncate" }, row.appDisplayName),
        m("span", { class: "text-faint w-28 shrink-0 truncate text-right" }, formatRecency(row.lastActiveMs, nowMs)),
      ],
    );
  }

  /** An "Open new <app>" row in a search's machine table: the tile restated as a row, with a "+" for
   *  a glyph, since the point of the row is that the thing does not exist yet. */
  function actionRow(tile: LaunchTile, attrs: NewTabLauncherAttrs): m.Vnode {
    const isDisabled = attrs.isAwaitingCreate === true;
    return m(
      "button",
      {
        key: `${tile.app.name}:${tile.action.id}`,
        type: "button",
        "data-launch": `${tile.app.name}:${tile.action.id}`,
        "aria-disabled": isDisabled ? "true" : undefined,
        class:
          "new-tab-launcher-action-row " + ROW_CLASS + (isDisabled ? "text-faint cursor-not-allowed" : "text-primary"),
        onclick: isDisabled ? undefined : () => attrs.onRunAction(tile.app, tile.action.id, {}),
      },
      [
        m(
          "span",
          { class: "text-faint flex w-5 shrink-0 items-center justify-center" },
          m.trust(launcherIcon("plus", GLYPH_SIZE)),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, actionRowLabel(tile)),
        m("span", { class: "text-faint w-24 shrink-0 truncate" }, tile.app.display_name),
        m("span", { class: "w-28 shrink-0" }),
      ],
    );
  }

  function sectionView(
    section: LauncherSection,
    attrs: NewTabLauncherAttrs,
    nowMs: number,
    actionTiles: readonly LaunchTile[],
  ): m.Vnode {
    const hiddenApps = hiddenAppsBySection[section.key];
    const visible = sortRowsByRecency(filterRowsByApp(section.rows, hiddenApps));
    // The action rows follow the same filter: an unchecked app hides what it could open too.
    const visibleActions = actionTiles.filter((tile) => !hiddenApps.has(tile.app.name));
    const nothingHere =
      section.key === "on-machine" ? "Nothing else is running on this machine." : "Nothing is in this project yet.";
    const emptyMessage = section.rows.length === 0 ? nothingHere : "No tabs match this filter.";

    return m("section", { class: "new-tab-launcher-section mt-6", "data-section": section.key }, [
      m("div", { class: "relative mb-1 flex h-6 items-center justify-between px-2" }, [
        m("h2", { class: SECTION_HEADING_CLASS }, section.title),
        m(
          "button",
          {
            type: "button",
            "aria-expanded": openFilterFor === section.key ? "true" : "false",
            [FILTER_TOGGLE_ATTR]: "",
            class: buttonClass("ghost", { icon: true, xs: true }),
            onclick: () => {
              openFilterFor = openFilterFor === section.key ? null : section.key;
            },
            ...hoverTooltipAttrs("Filter by app"),
          },
          m.trust(launcherIcon("filter", GLYPH_SIZE)),
        ),
        openFilterFor === section.key ? filterMenu(section) : null,
      ]),
      visibleActions.map((tile) => actionRow(tile, attrs)),
      visible.length === 0 && visibleActions.length === 0
        ? m("p", { class: "text-faint px-2 py-1 text-(length:--font-size-row)" }, emptyMessage)
        : visible.map((row) => memberRow(row, nowMs, attrs.onOpenRow)),
    ]);
  }

  // ---------- "Open new" ----------

  function tileView(tile: LaunchTile, attrs: NewTabLauncherAttrs): m.Vnode {
    const isDisabled = attrs.isAwaitingCreate === true;
    const run = (): void => attrs.onRunAction(tile.app, tile.action.id, {});
    return m(
      "div",
      {
        key: `${tile.app.name}:${tile.action.id}`,
        class:
          "border-default flex h-9 min-w-0 flex-1 items-stretch overflow-hidden rounded-lg border" +
          (isDisabled ? " text-faint" : " text-primary"),
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
              "text-(length:--font-size-row) font-medium " +
              (isDisabled ? "cursor-not-allowed" : "hover:bg-fill-hover cursor-pointer"),
            onclick: isDisabled ? undefined : run,
            ...(isDisabled ? {} : hoverTooltipAttrs(tile.action.label)),
          },
          [
            m("span", { class: "text-faint flex shrink-0 items-center" }, m.trust(appGlyph(tile.app.name))),
            m("span", { class: "min-w-0 truncate" }, tile.app.display_name),
          ],
        ),
      ],
    );
  }

  function tileRow(tiles: readonly LaunchTile[], attrs: NewTabLauncherAttrs, marker: string): m.Vnode {
    return m(
      "div",
      { class: `${marker} flex flex-wrap gap-2 px-2` },
      tiles.map((tile) => tileView(tile, attrs)),
    );
  }

  function openNewSection(attrs: NewTabLauncherAttrs): m.Vnode {
    const { leading, other } = splitLaunchTiles(attrs.tiles);
    return m("section", { class: "new-tab-launcher-open-new" }, [
      m(
        "h2",
        { class: `${SECTION_HEADING_CLASS} mb-2 px-2` },
        attrs.isAwaitingCreate === true ? STARTING_TITLE : OPEN_NEW_TITLE,
      ),
      attrs.tiles.length === 0
        ? m(
            "p",
            { class: "text-faint px-2 py-1 text-(length:--font-size-row)" },
            "No apps are registered on this machine yet.",
          )
        : [
            leading.length === 0 ? null : tileRow(leading, attrs, "new-tab-launcher-tiles-leading"),
            other.length === 0
              ? null
              : m(
                  "div",
                  { class: leading.length === 0 ? "" : "mt-2" },
                  tileRow(other, attrs, "new-tab-launcher-tiles-other"),
                ),
          ],
    ]);
  }

  // ---------- "Start something" ----------

  function startTile(option: StartOption, attrs: NewTabLauncherAttrs): m.Vnode {
    const isCatalogOffered = attrs.catalog.kind !== "disabled";
    const promptStart = promptStartDisabling(attrs);
    const isDisabled = option.prompt === null ? !isCatalogOffered : promptStart.isDisabled;
    const disabledReason = option.prompt === null ? TEMPLATES_NOT_OFFERED_REASON : promptStart.reason;
    const pick = (): void => {
      if (option.prompt === null) {
        query = "";
        isScrollToTemplatesPending = true;
        return;
      }
      startChat(attrs, option.prompt);
    };
    return m(
      "button",
      {
        key: option.key,
        type: "button",
        "data-start": option.key,
        "aria-disabled": isDisabled ? "true" : undefined,
        class:
          "new-tab-start-tile flex h-full flex-col rounded-xl border border-default bg-surface p-4 text-left " +
          (isDisabled ? "cursor-not-allowed text-faint" : "cursor-pointer text-primary hover:bg-fill-hover"),
        onclick: isDisabled ? undefined : pick,
        ...(isDisabled && disabledReason !== null ? hoverTooltipAttrs(disabledReason) : {}),
      },
      [
        m(
          "span",
          { class: "flex shrink-0 items-center " + (isDisabled ? "text-faint" : "text-secondary") },
          m.trust(startGlyph(option, START_GLYPH_SIZE)),
        ),
        m("span", { class: "type-label mt-3 block" }, option.title),
        m(
          "span",
          { class: "type-helper mt-1 block " + (isDisabled ? "text-faint" : "text-secondary") },
          option.description,
        ),
      ],
    );
  }

  function startGrid(options: readonly StartOption[], attrs: NewTabLauncherAttrs): m.Vnode {
    return m(
      "div",
      { class: "grid grid-cols-3 gap-3 px-2" },
      options.map((option) => startTile(option, attrs)),
    );
  }

  /** The section around a grid of intent tiles, with ``footer`` (the "See more" control) under the grid. */
  function startSomethingSection(
    options: readonly StartOption[],
    attrs: NewTabLauncherAttrs,
    footer: m.Vnode | null,
  ): m.Vnode {
    return m("section", { class: "new-tab-start-something mt-6" }, [
      m("h2", { class: `${SECTION_HEADING_CLASS} mb-2 px-2` }, START_SOMETHING_TITLE),
      startGrid(options, attrs),
      footer,
    ]);
  }

  /** The resting page's intents: a page at a time, with "See more" until every tile is shown. */
  function pagedStartSomethingSection(attrs: NewTabLauncherAttrs): m.Vnode {
    const seeMore = hasMoreStartOptions(startShownCount, START_OPTIONS.length)
      ? m("div", { class: "mt-2 flex justify-end px-2" }, [
          m(
            Button,
            {
              variant: "ghost",
              sm: true,
              extra: "new-tab-start-more",
              onclick: () => {
                startShownCount = nextStartCount(startShownCount, START_OPTIONS.length);
              },
            },
            SEE_MORE_LABEL,
          ),
        ])
      : null;
    return startSomethingSection(visibleStartOptions(START_OPTIONS, startShownCount), attrs, seeMore);
  }

  // ---------- "Start from a template" ----------

  function openDetail(template: CatalogTemplate): void {
    detailTemplate = template;
  }

  function templatesStatus(message: string): m.Vnode {
    return m("p", { class: "new-tab-templates-status text-faint px-2 py-1 text-(length:--font-size-row)" }, message);
  }

  function templatesSection(catalog: TemplateCatalogState): m.Vnode | null {
    if (catalog.kind === "disabled") return null;
    let body: m.Children;
    switch (catalog.kind) {
      case "loading":
        body = templatesStatus(TEMPLATES_LOADING_MESSAGE);
        break;
      case "failed":
        body = templatesStatus(TEMPLATES_FAILED_MESSAGE);
        break;
      case "loaded":
        body = m(TemplateShelves, { shelves: resolveShelves(catalog.catalog), onPick: openDetail });
        break;
    }
    return m(
      "section",
      {
        class: "new-tab-templates mt-10",
        oncreate: (vnode: m.VnodeDOM) => scrollToTemplatesIfPending(vnode.dom as HTMLElement),
        onupdate: (vnode: m.VnodeDOM) => scrollToTemplatesIfPending(vnode.dom as HTMLElement),
      },
      [m("h2", { class: `${SECTION_HEADING_CLASS} px-2` }, TEMPLATES_TITLE), body],
    );
  }

  function scrollToTemplatesIfPending(section: HTMLElement): void {
    if (!isScrollToTemplatesPending) return;
    isScrollToTemplatesPending = false;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- search results ----------

  function searchResults(attrs: NewTabLauncherAttrs, nowMs: number): m.Children {
    const trimmed = query.trim();
    const actionTiles = searchTiles(attrs.tiles, trimmed);
    const rows = searchLauncherRows(attrs.rows, trimmed);
    const starts = searchStartOptions(START_OPTIONS, trimmed);
    const templates = attrs.catalog.kind === "loaded" ? searchTemplates(attrs.catalog.catalog.templates, trimmed) : [];

    if (actionTiles.length === 0 && rows.length === 0 && starts.length === 0 && templates.length === 0) {
      return m("p", { class: "new-tab-launcher-no-matches mt-6 px-2 type-body text-secondary" }, [
        "Nothing matches “",
        m("span", { class: "text-primary" }, trimmed),
        "”.",
      ]);
    }

    return [
      actionTiles.length === 0 && rows.length === 0
        ? null
        : sectionView({ key: "on-machine", title: ON_MACHINE_TITLE, rows }, attrs, nowMs, actionTiles),
      starts.length === 0 ? null : startSomethingSection(starts, attrs, null),
      templates.length === 0
        ? null
        : m("section", { class: "new-tab-templates mt-6" }, [
            m("h2", { class: `${SECTION_HEADING_CLASS} mb-2 px-2` }, SEARCH_TEMPLATES_TITLE),
            m(
              "div",
              { class: "grid grid-cols-4 gap-6 px-2" },
              templates.map((template) =>
                m(TemplateCard, { key: template.slug, template, isFill: true, onPick: openDetail }),
              ),
            ),
          ]),
    ];
  }

  // ---------- the page ----------

  function restingPage(attrs: NewTabLauncherAttrs, nowMs: number): m.Children {
    return [
      openNewSection(attrs),
      restingSections(attrs.rows, attrs.memberRows, attrs.isEverything).map((section) =>
        sectionView(section, attrs, nowMs, []),
      ),
      // The page's one real break: above it is what you already have, below it is what you could start.
      m("div", { class: "new-tab-launcher-rule mt-6 border-t border-dashed border-default" }),
      pagedStartSomethingSection(attrs),
      templatesSection(attrs.catalog),
    ];
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const nowMs = attrs.nowMs ?? Date.now();
      const promptStart = promptStartDisabling(attrs);

      return m("div", { class: "new-tab-launcher bg-surface h-full w-full overflow-y-auto px-6 py-5" }, [
        m("div", { class: "mx-auto w-full max-w-4xl pb-12" }, [
          m("div", { class: "mb-6 px-2" }, searchField()),
          isSearching() ? searchResults(attrs, nowMs) : restingPage(attrs, nowMs),
        ]),
        detailTemplate === null
          ? null
          : m(TemplateDetailModal, {
              template: detailTemplate,
              isStartDisabled: promptStart.isDisabled,
              startDisabledReason: promptStart.reason,
              onClose: () => {
                detailTemplate = null;
              },
              onAdopt: (template: CatalogTemplate) => {
                detailTemplate = null;
                startChat(attrs, adoptTemplateMessage(template));
              },
              onCreateMachine: (template: CatalogTemplate) => {
                detailTemplate = null;
                startChat(attrs, createMachineFromTemplateMessage(template));
              },
            }),
      ]);
    },
  };
}
