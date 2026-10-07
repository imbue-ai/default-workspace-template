/**
 * The theme gallery's page (docs/system/blueprint/workspace-themes/, section 8.2): every part of contract 1 in its
 * states, drawn by the shell's own components under the named theme, with every app's icon. `gallery/index.ts`
 * loads it at `/theme-gallery?theme=<id>`.
 *
 * A window here is laid over a stand-in page, as a live window is laid over its app's frame, so a theme that
 * paints over the page shows here as a page you cannot see.
 */

import m from "mithril";
import { Button, type ButtonVariant } from "@imbue/workspace-ui/src/components/Button";
import { badgeClass } from "@imbue/workspace-ui/src/components/Badge";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import {
  MODAL_ACTIONS_CLASS,
  MODAL_CARD_CLASS,
  MODAL_HEADER_CLASS,
  MODAL_MESSAGE_CLASS,
  MODAL_TITLE_CLASS,
} from "@imbue/workspace-ui/src/components/Modal";
import { menuCardClass, menuDividerClass, menuRowClass } from "@imbue/workspace-ui/src/components/menu";
import {
  BADGE_PART,
  FIELD_PART,
  LIST_ROW_PART,
  MENU_ITEM_PART,
  MENU_PART,
  MENU_SEPARATOR_PART,
  PARTS,
  TILE_PART,
  partAttrs,
} from "@imbue/workspace-ui/src/themes/parts";
import type { AppRecord } from "../model/records";
import { chromeOf, resolveDesktopTheme, type ThemeCatalog, type ThemeRecord } from "../model/themes";
import { appGlyph } from "./glyphs";
import { TitleBar } from "./TitleBar";

const BUTTON_VARIANTS: readonly ButtonVariant[] = [
  "primary",
  "secondary",
  "ghost",
  "destructive",
  "ghost-destructive",
];
const TERMINAL_TOKENS = [
  "--term-background",
  "--term-foreground",
  "--term-cursor",
  "--term-selection",
  ...Array.from({ length: 16 }, (_, i) => `--term-ansi-${i}`),
];

export interface GalleryState {
  /** The theme the gallery is about: its name, revision, and problems head the page. */
  readonly theme: ThemeRecord;
  /** The theme the page wears: the theme itself, or the standard look for an unavailable one, whose files are not
   *  served. */
  readonly shown: ThemeRecord;
  readonly apps: readonly AppRecord[];
}

/** The gallery of the theme `requested` names, or null when the catalog has no such theme. */
export function galleryStateOf(
  catalog: ThemeCatalog,
  requested: string,
  apps: readonly AppRecord[],
): GalleryState | null {
  const theme = catalog.themes.find((candidate) => candidate.id === requested);
  if (theme === undefined) return null;
  return { theme, shown: resolveDesktopTheme(catalog, theme.id), apps: apps.filter((app) => !app.internal) };
}

function section(title: string, ...children: m.Children[]): m.Vnode {
  return m("section", { class: "mb-8" }, [m("h2", { class: "type-section mb-3 text-secondary" }, title), ...children]);
}

function windowSpecimen(state: GalleryState, isFocused: boolean, title: string): m.Vnode {
  const app = state.apps[0];
  return m("div", { class: "relative h-56 w-96" }, [
    // The stand-in page, under the window as an app's frame is under a live window.
    m(
      "div",
      {
        "data-gallery-page": "",
        class: "absolute inset-x-0 bottom-0 top-(--desk-title-bar-height) bg-chat p-3 type-body",
      },
      "The app's page shows through here.",
    ),
    m(
      "div",
      { ...partAttrs("window"), "data-focused": isFocused ? "true" : "false", class: "window absolute inset-0" },
      m(
        "div",
        {
          ...partAttrs("window-frame"),
          class:
            "window-frame flex h-full w-full flex-col overflow-hidden rounded-(--desk-window-radius) " +
            "shadow-(--desk-window-shadow)",
        },
        [
          m(TitleBar, {
            title,
            app,
            state: "NORMAL",
            isFocused,
            isMenuOpen: false,
            sizeMenuTrigger: {},
            onControl: () => undefined,
            onDoubleClick: () => undefined,
            chrome: chromeOf(state.shown),
          }),
          m("div", { ...partAttrs("window-content"), class: "window-content relative min-h-0 flex-1" }),
        ],
      ),
    ),
  ]);
}

function buttons(): m.Children {
  return m(
    "div",
    { class: "flex flex-col gap-2" },
    BUTTON_VARIANTS.map((variant) =>
      m("div", { class: "flex items-center gap-2" }, [
        m(Button, { variant }, variant),
        m(Button, { variant, disabled: true }, "disabled"),
        m(Button, { variant, icon: true, sm: true, "aria-label": variant }, m.trust("&#9733;")),
      ]),
    ),
  );
}

function fields(): m.Children {
  return m("div", { class: "flex w-80 flex-col gap-2" }, [
    m("input", { class: inputClass(), ...FIELD_PART, type: "text", value: "A field" }),
    m("input", { class: inputClass(), ...FIELD_PART, type: "text", placeholder: "A placeholder" }),
    m("textarea", { class: inputClass(), ...FIELD_PART, rows: 2 }, "A text area"),
    m("label", { class: "flex items-center gap-2 type-body" }, [
      m("input", { type: "checkbox", checked: true }),
      "A checkbox",
    ]),
    m("select", { ...partAttrs("select") }, [m("option", "A choice"), m("option", "Another")]),
  ]);
}

function menu(): m.Children {
  return m("div", { class: `${menuCardClass("relative w-56")}`, ...MENU_PART }, [
    m("button", { class: menuRowClass(), ...MENU_ITEM_PART, type: "button" }, "Open"),
    m("button", { class: menuRowClass(), ...MENU_ITEM_PART, type: "button" }, "Rename"),
    m("div", { class: menuDividerClass(), ...MENU_SEPARATOR_PART, role: "separator" }),
    m("button", { class: menuRowClass(), ...MENU_ITEM_PART, type: "button", "aria-disabled": "true" }, "Unavailable"),
  ]);
}

function dialog(): m.Children {
  return m("div", { class: `${MODAL_CARD_CLASS} relative`, ...partAttrs("dialog") }, [
    m("div", { class: MODAL_HEADER_CLASS, ...partAttrs("dialog-header") }, [
      m("h3", { class: MODAL_TITLE_CLASS, ...partAttrs("dialog-title") }, "A dialog"),
    ]),
    m("p", { class: MODAL_MESSAGE_CLASS }, "What the dialog asks, in a sentence or two."),
    m("input", { class: inputClass({ extra: "mb-3" }), ...FIELD_PART, type: "text", value: "Its field" }),
    m("div", { class: MODAL_ACTIONS_CLASS, ...partAttrs("dialog-actions") }, [
      m(Button, {}, "Cancel"),
      m(Button, { variant: "primary" }, "Save"),
    ]),
  ]);
}

function taskbar(state: GalleryState): m.Children {
  return m(
    "div",
    {
      ...partAttrs("taskbar"),
      class: "taskbar flex h-(--desk-taskbar-height) items-center gap-2 bg-(--desk-taskbar-surface) px-2",
    },
    [
      m(
        "div",
        {
          ...partAttrs("launcher-field"),
          class:
            "launcher-field flex h-9 w-(--desk-launcher-field-width) max-w-[40vw] shrink-0 items-center overflow-hidden rounded-full border border-strong bg-surface px-3 type-body whitespace-nowrap text-faint",
        },
        "Open an app or send a message",
      ),
      ...state.apps.slice(0, 3).map((app, index) =>
        m(
          "div",
          {
            ...partAttrs("taskbar-entry"),
            "data-focused": index === 0 ? "true" : "false",
            class:
              "taskbar-entry flex h-(--desk-taskbar-entry-size) items-center gap-1.5 rounded-md p-1 pr-2 " +
              (index === 0 ? "bg-fill-active" : ""),
          },
          [m.trust(appGlyph(app, 20)), m("span", app.display_name)],
        ),
      ),
    ],
  );
}

function desktop(state: GalleryState): m.Children {
  return m(
    "div",
    {
      ...partAttrs("desktop"),
      class: "flex flex-wrap gap-6 bg-(--desk-backdrop) bg-cover bg-center bg-(image:--desk-default-wallpaper) p-6",
    },
    state.apps.map((app) =>
      m("div", { ...partAttrs("shortcut"), class: "flex w-24 flex-col items-center gap-1 text-center" }, [
        m.trust(appGlyph(app, 48)),
        m(
          "span",
          { ...partAttrs("shortcut-label"), class: "relative w-full [filter:var(--desk-shortcut-label-shadow)]" },
          m("span", { class: "rounded px-1 type-helper text-on-accent" }, app.display_name),
        ),
      ]),
    ),
  );
}

function tilesAndRows(): m.Children {
  return m("div", { class: "flex gap-6" }, [
    m("div", { class: "grid w-96 grid-cols-2 gap-3" }, [
      m(
        "button",
        { ...TILE_PART, type: "button", class: "rounded-xl border border-default bg-surface p-4 text-left" },
        [
          m("div", { class: "type-label" }, "A tile"),
          m("div", { class: "type-helper text-secondary" }, "That opens something."),
        ],
      ),
      m(
        "button",
        {
          ...TILE_PART,
          type: "button",
          "aria-pressed": "true",
          class: "rounded-xl border border-accent bg-surface p-4 text-left",
        },
        [
          m("div", { class: "type-label" }, "A chosen tile"),
          m("div", { class: "type-helper text-secondary" }, "Pressed."),
        ],
      ),
    ]),
    m("div", { class: "flex w-56 flex-col gap-1 bg-sidebar p-2" }, [
      m(
        "div",
        { ...LIST_ROW_PART, "aria-current": "true", class: "rounded-md bg-fill-active px-2 py-1.5 type-body" },
        "The current row",
      ),
      m("div", { ...LIST_ROW_PART, class: "rounded-md px-2 py-1.5 type-body hover:bg-fill-hover" }, "Another row"),
      m("div", { class: "mt-2 flex gap-2" }, [
        m("span", { class: badgeClass("neutral"), ...BADGE_PART }, "badge"),
        m("span", { class: badgeClass("accent"), ...BADGE_PART }, "accent"),
        m(
          "span",
          { ...partAttrs("tooltip"), class: "rounded-md bg-inverse px-2 py-1 type-helper text-on-accent" },
          "A tooltip",
        ),
      ]),
    ]),
  ]);
}

function terminalPalette(): m.Children {
  // A theme that sets no terminal colors leaves the terminal its own, so there is nothing to swatch.
  const isPaletteSet = getComputedStyle(document.documentElement).getPropertyValue(TERMINAL_TOKENS[0]).trim() !== "";
  if (!isPaletteSet) {
    return m(
      "p",
      { "data-gallery-terminal": "own", class: "type-helper text-faint" },
      "The terminal keeps its own colors.",
    );
  }
  return m(
    "div",
    { "data-gallery-terminal": "theme", class: "flex flex-wrap gap-1" },
    TERMINAL_TOKENS.map((token) =>
      m("span", {
        title: token,
        class: "h-6 w-6 border border-default",
        style: `background: var(${token}, transparent)`,
      }),
    ),
  );
}

export function ThemeGallery(): m.Component<{ state: GalleryState }> {
  return {
    view(vnode) {
      const { state } = vnode.attrs;
      const { theme } = state;
      return m("main", { "data-gallery-ready": theme.id, class: "h-dvh overflow-y-auto bg-page p-8 text-primary" }, [
        m("header", { class: "mb-8" }, [
          m("h1", { class: "type-heading-lg" }, theme.name),
          m("p", { class: "type-body text-secondary" }, theme.description),
          m("p", { class: "type-helper text-faint" }, `${theme.id} · revision ${theme.revision || "built in"}`),
          theme.problems.length === 0
            ? null
            : m(
                "ul",
                { "data-gallery-problems": "", class: "mt-2 type-helper text-danger" },
                theme.problems.map((problem) => m("li", problem)),
              ),
        ]),
        section(
          "Windows",
          m("div", { class: "flex flex-wrap gap-8" }, [
            windowSpecimen(state, true, "A focused window"),
            windowSpecimen(state, false, "A window in the background"),
          ]),
        ),
        section("Buttons", buttons()),
        section("Fields", fields()),
        section("Menus and dialogs", m("div", { class: "flex flex-wrap items-start gap-8" }, [menu(), dialog()])),
        section("The desktop", desktop(state)),
        section("The taskbar", taskbar(state)),
        section("Tiles, rows, badges, and tooltips", tilesAndRows()),
        section("The terminal", terminalPalette()),
        m("p", { class: "type-helper text-faint" }, `Contract 1 parts: ${PARTS.join(", ")}`),
      ]);
    },
  };
}
