/**
 * The New Tab launcher: a full-page panel replacing both the "+" dropdown and
 * the empty-state overlay.
 *
 * It answers one question -- what do you want in this pane -- in three parts:
 * "Open new" starts something from scratch, "In this project" jumps to
 * something the active view already shows, and "On this machine" reaches
 * everything else the machine holds, whether it is filed in other projects or
 * in none at all.
 *
 * Opening a row from "On this machine" ADDS it to the active project and takes
 * it from nowhere: a project is a view, membership is many-to-many, and the
 * same object may be shown by any number of projects at once. The launcher does
 * not perform that itself -- it only reports which half of the split the row
 * came from, and the caller shares it in (``shareMember``) before opening the
 * panel, because the caller is the one that owns panel creation. That is also
 * why nothing here knows about dockview: every input arrives as an attr and
 * every action leaves as a callback (the same idiom as Sidebar and
 * AllAppsPicker).
 *
 * Everything is the unfiltered view, so it has no member list to split against:
 * there, "In this project" would be the whole machine and "On this machine"
 * would be empty. The launcher renders the single machine-wide table instead
 * (see buildLauncherSections), and opening a row from it changes no membership.
 *
 * The list building, kind filtering and recency ordering are exported as pure
 * functions above the component so they can be tested without a DOM, and so the
 * machine enumeration stays shared with the sidebar (buildLauncherRows is built
 * on Projects' buildEverythingMembers).
 */

import m from "mithril";
import { buildEverythingMembers, partitionByMembership, serviceNameFromRef } from "../models/Projects";
import type { MachineInventory, MemberKind } from "../models/Projects";
import { serviceIconMarkup } from "./appIcon";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon } from "./icons";

/** The kinds of object the "Open new" tiles can start from scratch. Distinct
 *  from MemberKind: "files" has no member ref yet (nothing backs it), and the
 *  tiles never start a URL tab. */
export type LaunchKind = "chat" | "files" | "browser" | "terminal";

/** One object the launcher can open. */
export interface LauncherRow {
  ref: string;
  kind: MemberKind;
  label: string;
  // Epoch milliseconds of the object's last activity, or null when the machine
  // reports none (a terminal nobody has touched since boot). Nulls sort last
  // and render as a dash rather than as "just now".
  lastActiveMs: number | null;
}

/** The two tables the launcher renders. The key is stable per table so the
 *  per-table kind filter keeps its checkboxes across redraws. */
export type LauncherSectionKey = "in-project" | "on-machine";

export interface LauncherSection {
  key: LauncherSectionKey;
  title: string;
  // Every row the table holds, before the kind filter and the recency sort.
  rows: LauncherRow[];
  // Whether opening a row from here has to file it into the active project
  // first. True only for the "on this machine" half of a project's split:
  // rows the view already shows are already members, and Everything has no
  // member list to add to.
  filesIntoProject: boolean;
}

const OPEN_NEW_TITLE = "Open new";
const IN_PROJECT_TITLE = "In this project";
const ON_MACHINE_TITLE = "On this machine";

// The small-caps heading over each block. Uppercasing is the stylesheet's job,
// so the titles stay readable as text (and as test assertions).
const SECTION_HEADING_CLASS = "text-text-faint text-[11px] font-semibold tracking-wider uppercase";

/** The order kinds are offered in, in the filter menu and in kindsInRows. */
const KIND_ORDER: readonly MemberKind[] = ["chat", "browser", "terminal", "app", "url"];

/** What each kind is called in the launcher's kind column and filter menu. */
export const LAUNCHER_KIND_LABELS: Readonly<Record<MemberKind, string>> = {
  chat: "Chat",
  browser: "Browser",
  terminal: "Terminal",
  app: "App",
  url: "Page",
};

/**
 * Flatten the machine into launcher rows.
 *
 * Built on buildEverythingMembers so the "On this machine" table and
 * Everything's tab list enumerate the machine through one function, in one
 * order, and cannot drift apart. The projects showing each ref are dropped:
 * membership is many-to-many, so who else shows an object changes nothing about
 * opening it here. `lastActiveMsByRef` decorates the rows it has an entry for;
 * a ref missing from it simply has no known recency.
 */
export function buildLauncherRows(
  inventory: MachineInventory,
  lastActiveMsByRef: Readonly<Record<string, number>>,
): LauncherRow[] {
  return buildEverythingMembers(inventory, {}).map((member) => ({
    ref: member.ref,
    kind: member.kind,
    label: member.label,
    lastActiveMs: lastActiveMsByRef[member.ref] ?? null,
  }));
}

/**
 * Split the machine into the launcher's tables.
 *
 * A project shows some of the machine, so it gets the two-table split; opening
 * one of the rest adds it here without taking it from anywhere. Everything
 * shows all of it, so the split would degenerate into a full table beside an
 * empty one -- it gets the single machine-wide table instead. Input order is
 * preserved within each table (the recency sort is applied at render, per
 * table, after the kind filter).
 */
export function buildLauncherSections(
  rows: readonly LauncherRow[],
  memberRefs: readonly string[],
  isEverything: boolean,
): LauncherSection[] {
  if (isEverything) {
    return [{ key: "on-machine", title: ON_MACHINE_TITLE, rows: [...rows], filesIntoProject: false }];
  }
  const partition = partitionByMembership(rows, memberRefs);
  return [
    { key: "in-project", title: IN_PROJECT_TITLE, rows: partition.inProject, filesIntoProject: false },
    { key: "on-machine", title: ON_MACHINE_TITLE, rows: partition.onMachine, filesIntoProject: true },
  ];
}

/**
 * Drop the rows whose kind the user unchecked in this table's filter.
 *
 * The state is the set of HIDDEN kinds rather than shown ones, so the resting
 * state is the empty set -- everything shows -- and a kind that only appears on
 * the machine later starts visible instead of being silently filtered out by a
 * set that was captured before it existed.
 */
export function filterRowsByKind(rows: readonly LauncherRow[], hiddenKinds: ReadonlySet<MemberKind>): LauncherRow[] {
  return rows.filter((row) => !hiddenKinds.has(row.kind));
}

/**
 * Order rows most-recently-active first.
 *
 * Rows with no known recency go last: they are the least likely thing to be
 * reaching for, and ranking them as "epoch" would scatter them through the
 * list. Ties keep the order the machine listed them in (Array.sort is stable),
 * so a fleet of terminals with no recency at all stays in its natural order.
 */
export function sortRowsByRecency(rows: readonly LauncherRow[]): LauncherRow[] {
  return [...rows].sort((left, right) => {
    if (left.lastActiveMs === right.lastActiveMs) return 0;
    if (left.lastActiveMs === null) return 1;
    if (right.lastActiveMs === null) return -1;
    return right.lastActiveMs - left.lastActiveMs;
  });
}

/** The kinds present in a table, in KIND_ORDER. The filter menu is built from
 *  this rather than from every kind that exists, so a project with no browsers
 *  does not offer to hide browsers. */
export function kindsInRows(rows: readonly LauncherRow[]): MemberKind[] {
  const present = new Set(rows.map((row) => row.kind));
  return KIND_ORDER.filter((kind) => present.has(kind));
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const WEEK_MS = 7 * DAY_MS;

/**
 * The recency column's text: coarse and relative, since the exact minute an
 * object was last touched is never what the launcher is being read for.
 *
 * A future timestamp reads as "just now" rather than as a negative age -- the
 * machine's clock and the browser's can disagree by a little, and the launcher
 * is not the surface to surface that on.
 */
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

// Inner markup for the launcher's own glyphs, on the same 24x24 Feather grid as
// `icons.ts`, which has no entry for any of them. The rail draws four of the
// same shapes (see Sidebar's QUICK_ADD_PATHS) -- they belong in the shared
// table once something other than these two views wants them. A URL row is the
// one kind `icons.ts` already covers, so it uses `icon()` below.
const LAUNCHER_PATHS = {
  chat:
    '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7' +
    'a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
  files: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  browser:
    '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/>' +
    '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  app: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>',
  filter:
    '<line x1="4" y1="7" x2="20" y2="7"/><line x1="7" y1="12" x2="17" y2="12"/><line x1="10" y1="17" x2="14" y2="17"/>',
} as const;

/** Full <svg> string for one launcher glyph. */
function launcherIcon(name: keyof typeof LAUNCHER_PATHS, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${LAUNCHER_PATHS[name]}</svg>`
  );
}

// The size every glyph in the launcher is drawn at: the tiles, the row glyphs
// and the filter funnel.
const GLYPH_SIZE = 15;

/** The glyph for a row, by what the object is. */
function kindIconMarkup(kind: MemberKind): string {
  switch (kind) {
    case "chat":
      return launcherIcon("chat", GLYPH_SIZE);
    case "browser":
      return launcherIcon("browser", GLYPH_SIZE);
    case "terminal":
      return launcherIcon("terminal", GLYPH_SIZE);
    case "app":
      return launcherIcon("app", GLYPH_SIZE);
    case "url":
      return icon("external-link", { size: GLYPH_SIZE });
  }
}

/** The glyph one table row wears: a registered app's own icon when it has one,
 *  and the kind's built-in glyph otherwise. */
function rowIconMarkup(row: LauncherRow): string {
  const fallback = kindIconMarkup(row.kind);
  if (row.kind !== "app") return fallback;
  return serviceIconMarkup(serviceNameFromRef(row.ref), GLYPH_SIZE, fallback);
}

const OPEN_NEW_TILES: readonly { kind: LaunchKind; label: string }[] = [
  { kind: "chat", label: "Chat" },
  { kind: "files", label: "File viewer" },
  { kind: "browser", label: "Browser" },
  { kind: "terminal", label: "Terminal" },
];

// No file-viewer app exists on this machine yet, so the tile is present but
// cannot act. It is marked aria-disabled rather than `disabled`: a disabled
// button receives no pointer events in Chromium, which would swallow the very
// hover that explains why it does nothing.
const FILE_VIEWER_TOOLTIP = "A file viewer is coming — no app backs it yet";

export interface NewTabLauncherAttrs {
  // Everything the machine holds, already flattened (see buildLauncherRows).
  rows: readonly LauncherRow[];
  // The active view's member refs, which the machine list is split against.
  // Ignored when isEverything is set -- the unfiltered view has no member list.
  memberRefs: readonly string[];
  // Whether the active view is Everything, which renders the single
  // machine-wide table instead of the split.
  isEverything: boolean;
  // "Now" for the recency column. Defaults to the wall clock; passed in by
  // tests so the rendered ages are deterministic.
  nowMs?: number;
  // Start a new object of this kind in this pane. Never fired for "files".
  onOpenNew: (kind: LaunchKind) => void;
  // Open an object the active view already shows. Membership does not change.
  onOpenMember: (row: LauncherRow) => void;
  // Open an object the active view does not show yet. The caller shares it into
  // the active project first (shareMember, which adds it here and takes it from
  // nowhere) and then opens it. Never fired in Everything.
  onOpenFromMachine: (row: LauncherRow) => void;
}

export function NewTabLauncher(): m.Component<NewTabLauncherAttrs> {
  // Per table, the kinds the user unchecked. Hidden rather than shown so a kind
  // that appears later starts visible (see filterRowsByKind).
  const hiddenKindsBySection: Record<LauncherSectionKey, Set<MemberKind>> = {
    "in-project": new Set(),
    "on-machine": new Set(),
  };
  // Which table's filter menu is open, at most one at a time.
  let openFilterFor: LauncherSectionKey | null = null;
  // The open menu's element, so the outside-pointerdown listener can tell a
  // click inside the menu from one that dismisses it.
  let menuElement: HTMLElement | null = null;

  const closeFilterMenu = (): void => {
    openFilterFor = null;
    m.redraw();
  };

  const onDocumentPointerDown = (event: Event): void => {
    if (menuElement !== null && event.target instanceof Node && menuElement.contains(event.target)) return;
    closeFilterMenu();
  };

  const onDocumentKeyDown = (event: KeyboardEvent): void => {
    if (event.key === "Escape") closeFilterMenu();
  };

  /** The funnel menu for one table: one checkbox per kind the table holds. */
  function filterMenu(section: LauncherSection): m.Vnode {
    const hidden = hiddenKindsBySection[section.key];
    return m(
      "div",
      {
        class:
          "absolute top-full right-0 z-30 mt-1 min-w-[150px] rounded-lg border border-border bg-surface py-1 shadow-lg",
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
      kindsInRows(section.rows).map((kind) =>
        m(
          "label",
          {
            key: kind,
            class: "flex h-8 cursor-pointer items-center gap-2 px-3 text-[13px] text-text-primary hover:bg-bg-hover",
          },
          [
            m("input", {
              type: "checkbox",
              checked: !hidden.has(kind),
              onchange: () => {
                if (hidden.has(kind)) {
                  hidden.delete(kind);
                } else {
                  hidden.add(kind);
                }
              },
            }),
            LAUNCHER_KIND_LABELS[kind],
          ],
        ),
      ),
    );
  }

  /** One row: kind glyph, label, kind column, recency column. */
  function memberRow(row: LauncherRow, nowMs: number, onOpen: (row: LauncherRow) => void): m.Vnode {
    return m(
      "button",
      {
        key: row.ref,
        type: "button",
        class:
          "new-tab-launcher-row flex h-9 w-full cursor-pointer items-center gap-3 rounded-md px-2 text-left " +
          "text-[13px] text-text-primary hover:bg-bg-hover",
        onclick: () => onOpen(row),
      },
      [
        m(
          "span",
          { class: "text-text-faint flex w-5 shrink-0 items-center justify-center" },
          m.trust(rowIconMarkup(row)),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, row.label),
        m("span", { class: "text-text-faint w-24 shrink-0 truncate" }, LAUNCHER_KIND_LABELS[row.kind]),
        m(
          "span",
          { class: "text-text-faint w-28 shrink-0 truncate text-right" },
          formatRecency(row.lastActiveMs, nowMs),
        ),
      ],
    );
  }

  function sectionView(section: LauncherSection, attrs: NewTabLauncherAttrs, nowMs: number): m.Vnode {
    const visible = sortRowsByRecency(filterRowsByKind(section.rows, hiddenKindsBySection[section.key]));
    const onOpen = section.filesIntoProject ? attrs.onOpenFromMachine : attrs.onOpenMember;
    // Distinguish "there is nothing here" from "your filter hid all of it" --
    // the fix for each is different.
    const nothingHere = section.filesIntoProject
      ? "Nothing else is running on this machine."
      : "Nothing is in this project yet.";
    const emptyMessage = section.rows.length === 0 ? nothingHere : "No tabs match this filter.";

    return m("section", { key: section.key, class: "new-tab-launcher-section mt-6", "data-section": section.key }, [
      m("div", { class: "relative mb-1 flex h-6 items-center justify-between px-2" }, [
        m("h2", { class: SECTION_HEADING_CLASS }, section.title),
        m(
          "button",
          {
            type: "button",
            "aria-expanded": openFilterFor === section.key ? "true" : "false",
            class:
              "text-text-faint flex h-6 w-6 cursor-pointer items-center justify-center rounded " +
              "hover:bg-bg-hover hover:text-text-primary",
            onclick: () => {
              openFilterFor = openFilterFor === section.key ? null : section.key;
            },
            ...hoverTooltipAttrs("Filter by kind"),
          },
          m.trust(launcherIcon("filter", GLYPH_SIZE)),
        ),
        openFilterFor === section.key ? filterMenu(section) : null,
      ]),
      visible.length === 0
        ? m("p", { class: "text-text-faint px-2 py-1 text-[13px]" }, emptyMessage)
        : visible.map((row) => memberRow(row, nowMs, onOpen)),
    ]);
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const nowMs = attrs.nowMs ?? Date.now();
      const sections = buildLauncherSections(attrs.rows, attrs.memberRefs, attrs.isEverything);

      return m("div", { class: "new-tab-launcher bg-surface h-full w-full overflow-y-auto px-6 py-5" }, [
        m("h2", { class: `${SECTION_HEADING_CLASS} mb-2 px-2` }, OPEN_NEW_TITLE),
        m(
          "div",
          { class: "flex flex-wrap gap-2 px-2" },
          OPEN_NEW_TILES.map((tile) => {
            const isDisabled = tile.kind === "files";
            return m(
              "button",
              {
                key: tile.kind,
                type: "button",
                "aria-disabled": isDisabled ? "true" : undefined,
                class:
                  "new-tab-launcher-tile border-border flex h-11 items-center gap-2 rounded-lg border px-4 " +
                  "text-[13px] font-medium " +
                  (isDisabled
                    ? "text-text-faint cursor-not-allowed"
                    : "text-text-primary hover:bg-bg-hover cursor-pointer"),
                onclick: isDisabled ? undefined : () => attrs.onOpenNew(tile.kind),
                ...(isDisabled ? hoverTooltipAttrs(FILE_VIEWER_TOOLTIP) : {}),
              },
              [
                m(
                  "span",
                  { class: "text-text-faint flex shrink-0 items-center" },
                  m.trust(launcherIcon(tile.kind, GLYPH_SIZE)),
                ),
                tile.label,
              ],
            );
          }),
        ),
        sections.map((section) => sectionView(section, attrs, nowMs)),
      ]);
    },
  };
}
