/**
 * The launcher overlay (plan section 4.11): what focusing the taskbar's field opens above it.
 * Resting content, top to bottom: "Open new" (one tile per launch path of every non-internal
 * app, ranked apps first), "On this desktop" (the active desktop's windows by title, minimized
 * ones marked, omitted when empty), then under a dashed rule the two offers of things to start:
 * "Start something" (hardcoded intents, each a chat seeded with a prompt through the launch path
 * that declares a ``message`` param) and "Start from a template" (the published catalog). Typing
 * swaps the sections for results: windows across every desktop (switching desktop on choice),
 * launch paths, intents, templates.
 *
 * Nothing here knows what any app is; the list building, filtering, and ordering are pure
 * functions exported for tests. Markers the e2e suite finds the overlay by are data attributes:
 * ``data-launcher-overlay``, ``data-launch="<app>:<launch>"``, ``data-launcher-window``,
 * ``data-section``.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import {
  NO_CHAT_APP_REASON,
  launchRowLabel,
  launchTilesOf,
  orderLaunchTiles,
  promptTargetOfTiles,
} from "../model/launch";
import type { LaunchTile } from "../model/launch";
import { MESSAGE_PARAM } from "../model/launch";
import type { AppRecord, Desktop, LaunchPath, WindowRecord } from "../model/records";
import { shortcutKey } from "../model/records";
import { matchesQuery } from "../model/search";
import { resolveShelves, searchTemplates } from "../model/TemplateCatalog";
import type { CatalogTemplate, TemplateCatalogState } from "../model/TemplateCatalog";
import { HOVER_GLYPH_GROUP, HOVER_SHADOW_SELF } from "./hoverLift";
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
import { appGlyph, glyph } from "./glyphs";

/** One window the launcher can raise: on which desktop, and what it is called. */
export interface LauncherWindowRow {
  readonly window: WindowRecord;
  readonly desktopId: string;
  readonly desktopName: string;
  readonly app: AppRecord | undefined;
  readonly title: string;
  readonly isMinimized: boolean;
}

const OPEN_NEW_TITLE = "Open new";
const ON_THIS_DESKTOP_TITLE = "On this desktop";
const WINDOWS_TITLE = "Windows";
const START_SOMETHING_TITLE = "Start something";
const TEMPLATES_TITLE = "Start from a template";
const SEARCH_TEMPLATES_TITLE = "Templates";
const SEE_MORE_LABEL = "See more";
const TEMPLATES_LOADING_MESSAGE = "Loading templates…";
const TEMPLATES_FAILED_MESSAGE = "Failed to load templates.";
const TEMPLATES_NOT_OFFERED_REASON = "No template catalog is configured on this machine";

const SECTION_HEADING_CLASS = "type-section text-faint";
const GLYPH_SIZE = 15;
const START_GLYPH_SIZE = 24;

/** Adopt a template into this machine: the first message of the chat "Make it mine" starts. */
export function adoptTemplateMessage(template: CatalogTemplate): string {
  return `/use-template ${template.repository_url}`;
}

/** Have a new machine made from a template: the first message of the chat that action starts. */
export function createMachineFromTemplateMessage(template: CatalogTemplate): string {
  return (
    `Please create a new Mind machine for me from the template at ${template.repository_url} ` +
    "(the minds-api skill can create one). Walk me through anything it needs from me, like permissions " +
    "or accounts, and tell me when it is ready."
  );
}

/** The windows a query finds: by title, desktop, or the app they belong to. */
export function searchWindowRows(rows: readonly LauncherWindowRow[], query: string): LauncherWindowRow[] {
  return rows.filter((row) =>
    matchesQuery(query, row.title, row.desktopName, row.app?.display_name ?? "", row.window.app),
  );
}

/** The "Open new" tiles a query finds: by the row text the match renders as, the app's names, or the label. */
export function searchTiles(tiles: readonly LaunchTile[], query: string): LaunchTile[] {
  return tiles.filter((tile) =>
    matchesQuery(query, launchRowLabel(tile), tile.app.display_name, tile.app.name, tile.launchPath.label),
  );
}

/** Every window of every desktop as a launcher row, the active desktop's first. */
export function windowRowsOf(
  desktops: readonly Desktop[],
  activeDesktopId: string | null,
  appByName: (name: string) => AppRecord | undefined,
  titleOf: (window: WindowRecord, app: AppRecord | undefined) => string,
  isMinimized: (desktopId: string, windowId: string) => boolean,
): LauncherWindowRow[] {
  const ordered = [...desktops].sort(
    (left, right) => Number(right.id === activeDesktopId) - Number(left.id === activeDesktopId),
  );
  return ordered.flatMap((desktop) =>
    desktop.windows.map((window) => {
      const app = appByName(window.app);
      return {
        window,
        desktopId: desktop.id,
        desktopName: desktop.name,
        app,
        title: titleOf(window, app),
        isMinimized: isMinimized(desktop.id, window.id),
      };
    }),
  );
}

export interface LauncherOverlayAttrs {
  readonly query: string;
  readonly apps: readonly AppRecord[];
  readonly windows: readonly LauncherWindowRow[];
  readonly activeDesktopId: string | null;
  readonly catalog: TemplateCatalogState;
  readonly isCompact: boolean;
  /** Run a launch path (a new window), with the params of a seeded prompt. */
  readonly onRunLaunch: (app: AppRecord, launchPath: LaunchPath, params: Readonly<Record<string, string>>) => void;
  readonly onPickWindow: (row: LauncherWindowRow) => void;
}

const ROW_CLASS =
  "launcher-row flex h-9 w-full cursor-pointer items-center gap-3 rounded-md px-2 text-left " +
  "text-(length:--font-size-row) hover:bg-fill-hover ";

export function LauncherOverlay(): m.Component<LauncherOverlayAttrs> {
  let startShownCount = START_PAGE_SIZE;
  let detailTemplate: CatalogTemplate | null = null;
  let isScrollToTemplatesPending = false;

  function tiles(attrs: LauncherOverlayAttrs): LaunchTile[] {
    return orderLaunchTiles(launchTilesOf(attrs.apps));
  }

  function promptStartDisabling(attrs: LauncherOverlayAttrs): { isDisabled: boolean; reason: string | null } {
    if (promptTargetOfTiles(tiles(attrs)) === null) return { isDisabled: true, reason: NO_CHAT_APP_REASON };
    return { isDisabled: false, reason: null };
  }

  function startChat(attrs: LauncherOverlayAttrs, message: string): void {
    const target = promptTargetOfTiles(tiles(attrs));
    if (target === null) return;
    attrs.onRunLaunch(target.app, target.launchPath, { [MESSAGE_PARAM]: message });
  }

  function tileView(tile: LaunchTile, attrs: LauncherOverlayAttrs): m.Vnode {
    const key = shortcutKey(tile.app.name, tile.launchPath.id);
    return m(
      "button",
      {
        key,
        type: "button",
        "data-launch": key,
        class:
          "launcher-tile flex h-9 min-w-0 shrink-0 items-center justify-center gap-2 overflow-hidden rounded-lg border " +
          "border-default px-4 text-(length:--font-size-row) font-medium text-primary hover:bg-fill-hover cursor-pointer " +
          (attrs.isCompact ? "w-[calc((100%-var(--spacing)*2)/2)]" : "w-[calc((100%-var(--spacing)*6)/4)]"),
        ...hoverTooltipAttrs(tile.launchPath.label),
        onclick: () => attrs.onRunLaunch(tile.app, tile.launchPath, {}),
      },
      [
        m("span", { class: "flex shrink-0 items-center text-faint" }, m.trust(appGlyph(tile.app, GLYPH_SIZE))),
        m(
          "span",
          { class: "min-w-0 truncate" },
          tile.app.launch_paths.length > 1 ? tile.launchPath.label : tile.app.display_name,
        ),
      ],
    );
  }

  function openNewSection(attrs: LauncherOverlayAttrs): m.Vnode {
    const ordered = tiles(attrs);
    return m("section", { "data-section": "open-new", class: "launcher-open-new" }, [
      m("h2", { class: `${SECTION_HEADING_CLASS} mb-2 px-2` }, OPEN_NEW_TITLE),
      ordered.length === 0
        ? m(
            "p",
            { class: "px-2 py-1 text-(length:--font-size-row) text-faint" },
            "No apps are registered on this machine yet.",
          )
        : m(
            "div",
            { class: "launcher-tiles flex flex-wrap gap-2 px-2" },
            ordered.map((tile) => tileView(tile, attrs)),
          ),
    ]);
  }

  function windowRow(row: LauncherWindowRow, attrs: LauncherOverlayAttrs, isCrossDesktop: boolean): m.Vnode {
    return m(
      "button",
      {
        key: row.window.id,
        type: "button",
        "data-launcher-window": row.window.id,
        "data-minimized": row.isMinimized ? "true" : "false",
        class: ROW_CLASS + (row.isMinimized ? "text-faint" : "text-primary"),
        onclick: () => attrs.onPickWindow(row),
      },
      [
        m(
          "span",
          { class: "flex w-5 shrink-0 items-center justify-center text-faint" },
          m.trust(appGlyph(row.app, GLYPH_SIZE)),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, row.title),
        m("span", { class: "w-24 shrink-0 truncate text-faint" }, row.app?.display_name ?? row.window.app),
        isCrossDesktop ? m("span", { class: "w-24 shrink-0 truncate text-right text-faint" }, row.desktopName) : null,
        row.isMinimized ? m("span", { class: "type-helper shrink-0 text-faint" }, "minimized") : null,
      ],
    );
  }

  function launchRow(tile: LaunchTile, attrs: LauncherOverlayAttrs): m.Vnode {
    const key = shortcutKey(tile.app.name, tile.launchPath.id);
    return m(
      "button",
      {
        key,
        type: "button",
        "data-launch": key,
        class: `launcher-launch-row ${ROW_CLASS} text-primary`,
        onclick: () => attrs.onRunLaunch(tile.app, tile.launchPath, {}),
      },
      [
        m(
          "span",
          { class: "flex w-5 shrink-0 items-center justify-center text-faint" },
          m.trust(glyph("plus", GLYPH_SIZE)),
        ),
        m(
          "span",
          { class: "min-w-0 flex-1 truncate" },
          tile.app.launch_paths.length > 1 ? tile.launchPath.label : launchRowLabel(tile),
        ),
        m("span", { class: "w-24 shrink-0 truncate text-faint" }, tile.app.display_name),
      ],
    );
  }

  function onThisDesktopSection(attrs: LauncherOverlayAttrs): m.Vnode | null {
    const rows = attrs.windows.filter((row) => row.desktopId === attrs.activeDesktopId);
    if (rows.length === 0) return null;
    return m("section", { "data-section": "on-this-desktop", class: "launcher-section mt-6" }, [
      m("h2", { class: `${SECTION_HEADING_CLASS} mb-1 px-2` }, ON_THIS_DESKTOP_TITLE),
      rows.map((row) => windowRow(row, attrs, false)),
    ]);
  }

  function startTile(option: StartOption, attrs: LauncherOverlayAttrs): m.Vnode {
    const isCatalogOffered = attrs.catalog.kind !== "disabled";
    const promptStart = promptStartDisabling(attrs);
    const isDisabled = option.prompt === null ? !isCatalogOffered : promptStart.isDisabled;
    const disabledReason = option.prompt === null ? TEMPLATES_NOT_OFFERED_REASON : promptStart.reason;
    const pick = (): void => {
      if (option.prompt === null) {
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
          "launcher-start-tile flex h-full flex-col rounded-xl border border-default bg-surface p-4 text-left " +
          (isDisabled ? "cursor-not-allowed text-faint" : `${HOVER_SHADOW_SELF} group cursor-pointer text-primary`),
        onclick: isDisabled ? undefined : pick,
        ...hoverTooltipAttrs(isDisabled ? disabledReason : null),
      },
      [
        m(
          "span",
          { class: "flex shrink-0 items-center" + (isDisabled ? " text-faint" : ` ${HOVER_GLYPH_GROUP}`) },
          m.trust(startGlyph(option, START_GLYPH_SIZE, !isDisabled)),
        ),
        m("span", { class: "type-label mt-3 block" }, option.title),
        m(
          "span",
          {
            class:
              "type-helper mt-1 block " +
              (isDisabled
                ? "text-faint"
                : "text-secondary transition-colors duration-300 ease-out group-hover:text-primary"),
          },
          option.description,
        ),
      ],
    );
  }

  function startSomethingSection(
    options: readonly StartOption[],
    attrs: LauncherOverlayAttrs,
    footer: m.Vnode | null,
  ): m.Vnode {
    return m("section", { "data-section": "start-something", class: "launcher-start-something mt-6" }, [
      m("h2", { class: `${SECTION_HEADING_CLASS} mb-2 px-2` }, START_SOMETHING_TITLE),
      m(
        "div",
        { class: "grid gap-3 px-2 " + (attrs.isCompact ? "grid-cols-1" : "grid-cols-3") },
        options.map((option) => startTile(option, attrs)),
      ),
      footer,
    ]);
  }

  function pagedStartSomethingSection(attrs: LauncherOverlayAttrs): m.Vnode {
    const seeMore = hasMoreStartOptions(startShownCount, START_OPTIONS.length)
      ? m("div", { class: "mt-2 flex justify-end px-2" }, [
          m(
            Button,
            {
              variant: "ghost",
              sm: true,
              extra: "launcher-start-more",
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

  function templatesStatus(message: string): m.Vnode {
    return m("p", { class: "launcher-templates-status px-2 py-1 text-(length:--font-size-row) text-faint" }, message);
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
        body = m(TemplateShelves, {
          shelves: resolveShelves(catalog.catalog),
          onPick: (template) => (detailTemplate = template),
        });
        break;
    }
    return m(
      "section",
      {
        "data-section": "templates",
        class: "launcher-templates mt-10",
        oncreate: (created: m.VnodeDOM) => scrollToTemplatesIfPending(created.dom as HTMLElement),
        onupdate: (updated: m.VnodeDOM) => scrollToTemplatesIfPending(updated.dom as HTMLElement),
      },
      [m("h2", { class: `${SECTION_HEADING_CLASS} px-2` }, TEMPLATES_TITLE), body],
    );
  }

  function scrollToTemplatesIfPending(section: HTMLElement): void {
    if (!isScrollToTemplatesPending) return;
    isScrollToTemplatesPending = false;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function searchResults(attrs: LauncherOverlayAttrs): m.Children {
    const trimmed = attrs.query.trim();
    const foundTiles = searchTiles(tiles(attrs), trimmed);
    const rows = searchWindowRows(attrs.windows, trimmed);
    const starts = searchStartOptions(START_OPTIONS, trimmed);
    const templates = attrs.catalog.kind === "loaded" ? searchTemplates(attrs.catalog.catalog.templates, trimmed) : [];
    // No templates section to scroll to in these results: a pick of the template tile has nowhere to go,
    // and must not be owed to the resting page later.
    if (templates.length === 0) isScrollToTemplatesPending = false;
    if (foundTiles.length === 0 && rows.length === 0 && starts.length === 0 && templates.length === 0) {
      return m("p", { class: "launcher-no-matches mt-6 px-2 type-body text-secondary" }, [
        "Nothing matches “",
        m("span", { class: "text-primary" }, trimmed),
        "”.",
      ]);
    }
    return [
      foundTiles.length === 0
        ? null
        : m("section", { "data-section": "open-new", class: "launcher-section" }, [
            m("h2", { class: `${SECTION_HEADING_CLASS} mb-1 px-2` }, OPEN_NEW_TITLE),
            foundTiles.map((tile) => launchRow(tile, attrs)),
          ]),
      rows.length === 0
        ? null
        : m("section", { "data-section": "windows", class: "launcher-section mt-6 first:mt-0" }, [
            m("h2", { class: `${SECTION_HEADING_CLASS} mb-1 px-2` }, WINDOWS_TITLE),
            rows.map((row) => windowRow(row, attrs, true)),
          ]),
      starts.length === 0 ? null : startSomethingSection(starts, attrs, null),
      templates.length === 0
        ? null
        : m(
            "section",
            {
              "data-section": "templates",
              class: "launcher-templates mt-6",
              oncreate: (created: m.VnodeDOM) => scrollToTemplatesIfPending(created.dom as HTMLElement),
              onupdate: (updated: m.VnodeDOM) => scrollToTemplatesIfPending(updated.dom as HTMLElement),
            },
            [
              m("h2", { class: `${SECTION_HEADING_CLASS} mb-2 px-2` }, SEARCH_TEMPLATES_TITLE),
              m(
                "div",
                { class: "grid gap-6 px-2 " + (attrs.isCompact ? "grid-cols-2" : "grid-cols-4") },
                templates.map((template) =>
                  m(TemplateCard, {
                    key: template.slug,
                    template,
                    isFill: true,
                    onPick: (picked) => (detailTemplate = picked),
                  }),
                ),
              ),
            ],
          ),
    ];
  }

  function restingPage(attrs: LauncherOverlayAttrs): m.Children {
    return [
      openNewSection(attrs),
      onThisDesktopSection(attrs),
      m("div", { class: "launcher-rule mt-6 border-t border-dashed border-default" }),
      pagedStartSomethingSection(attrs),
      templatesSection(attrs.catalog),
    ];
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const promptStart = promptStartDisabling(attrs);
      return m(
        "div",
        {
          "data-launcher-overlay": "",
          class:
            "launcher-overlay @container absolute inset-x-0 bottom-0 z-(--z-dropdown) max-h-[75%] overflow-y-auto " +
            "border-t border-default bg-surface px-6 py-5 shadow-overlay " +
            (attrs.isCompact ? "" : "mx-auto max-w-4xl rounded-t-xl border-x"),
        },
        [
          m("div", { class: "pb-6" }, attrs.query.trim() !== "" ? searchResults(attrs) : restingPage(attrs)),
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
        ],
      );
    },
  };
}
