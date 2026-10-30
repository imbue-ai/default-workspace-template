/**
 * The Theming group: which theme the page wears, and every part of the theme contract
 * (docs/system/blueprint/workspace-themes/, section 4.2) with where the catalog shows it. The page wears the
 * workspace's theme like any page built on the tokens, so under a theme every swatch and control above is that
 * theme's.
 */

import m from "mithril";

import { PARTS, type Part } from "@imbue/workspace-ui/src/themes/parts";
import { STANDARD_THEME_ID } from "@imbue/workspace-ui/src/themes/themeClient";

import { relayToLoadedFrame, THEME_RELAY_ATTRIBUTE } from "../models/themeRelay";
import { caption, code, type CatalogGroup, type CatalogSection } from "./catalog";

/** The catalog section each part is shown in; a part missing here is drawn only by the shell or by another app. */
export const PART_SECTIONS: Partial<Record<Part, string>> = {
  button: "buttons",
  field: "fields",
  select: "pickers",
  menu: "menu",
  "menu-item": "menu",
  "menu-separator": "menu",
  dialog: "dialogs",
  "dialog-header": "dialogs",
  "dialog-title": "dialogs",
  "dialog-actions": "dialogs",
  tooltip: "tooltips",
  badge: "badges",
  tile: "page-kit",
  "list-row": "page-kit",
};

/** Where a part the catalog does not show is drawn. */
function drawnBy(part: Part): string {
  if (part === "avatar") return "the pinned chat's avatar";
  return "the desktop and its windows (the shell)";
}

/** The theme the page wears now: what the theme client marked on ``<html>``. */
export function wornThemeId(doc: Document = document): string {
  return doc.documentElement.getAttribute("data-ui-theme") ?? STANDARD_THEME_ID;
}

function jumpTo(id: string): (event: MouseEvent) => void {
  return (event) => {
    event.preventDefault();
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
}

function partsSection(labelOf: (sectionId: string) => string): CatalogSection {
  return {
    id: "theme-parts",
    toc: "Parts",
    title: "Parts a theme can style",
    desc: [
      "A theme styles the interface only through the tokens above and these named parts, each marked ",
      code('data-part="<name>"'),
      " in the markup, never through an app's class names. Pick a theme in Desktop settings and every swatch and control on this page shows it; the shell's theme gallery shows the window chrome and the desktop.",
    ],
    body: () =>
      m(
        "div",
        { class: "grid grid-cols-[minmax(150px,auto)_1fr] items-center gap-x-6 gap-y-1.5" },
        PARTS.flatMap((part) => {
          const section = PART_SECTIONS[part];
          return [
            m("div", { id: `part-${part}` }, caption(part)),
            section === undefined
              ? m("span", { class: "type-helper text-faint" }, drawnBy(part))
              : m(
                  "a",
                  {
                    href: `#${section}`,
                    "data-part-section": section,
                    class: "type-helper text-accent hover:underline",
                    onclick: jumpTo(section),
                  },
                  `Shown under ${labelOf(section)}`,
                ),
          ];
        }),
      ),
  };
}

function pageKitSection(): CatalogSection {
  return {
    id: "page-kit",
    toc: "Plain pages",
    title: "Plain pages: the page kit",
    desc: [
      "An app page built as plain HTML rather than on the shared components loads the page kit, ",
      code("/_static/workspace_theme.css"),
      " and ",
      code("/_static/workspace_theme.js"),
      ", from its own origin. The stylesheet gives its plain buttons, fields and selects the standard look and the script wears the workspace's theme, so a theme dresses them as their parts. This frame is such a page: plain markup, no classes, the kit and nothing else.",
    ],
    body: () =>
      m("iframe", {
        id: "page-kit-frame",
        src: "/page-kit-sample",
        title: "A plain page wearing the page kit",
        [THEME_RELAY_ATTRIBUTE]: "",
        class: "block h-[620px] w-full rounded-lg border border-default bg-page",
        onload: (event: Event) => relayToLoadedFrame(event.currentTarget as HTMLIFrameElement),
      }),
  };
}

/** The Theming group; ``labelOf`` names a catalog section by its contents-list label. */
export function themingGroup(labelOf: (sectionId: string) => string): CatalogGroup {
  return {
    id: "theming",
    label: "Theming",
    description: "How a theme reaches what is on this page.",
    sections: [partsSection(labelOf), pageKitSection()],
  };
}
