/**
 * The theme chooser (docs/system/blueprint/workspace-themes/): the desktop's theme and the workspace's default,
 * with a tile per theme pictured by how it draws the Files icon, "Make your own..." drafting the design prompt
 * into the chat, and a live preview while the desktop is on screen. Opened from "Change theme..." on the
 * backdrop's context menu (the desktop settings dialog it grew up in became the wallpaper chooser when the
 * multiplayer overview took the desktop's name; its colour and glyph went away).
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { MODAL_LABEL_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import { TILE_PART } from "@imbue/workspace-ui/src/themes/parts";
import type { AppRecord, Desktop } from "../model/records";
import { availableTheme, resolveDesktopTheme, type ThemeCatalog, type ThemeRecord } from "../model/themes";
import { appIconMarkupForApp } from "./components/appIcon";
import { glyph as genericGlyph } from "./glyphs";

export interface ThemeChooserDialogAttrs {
  readonly desktop: Desktop;
  readonly onSave: (themeChoice: ThemeChoice) => Promise<void>;
  readonly onCancel: () => void;
  /** The workspace's themes; null until they have been read. */
  readonly themes: ThemeCatalog | null;
  /** Draft the theme prompt into the chat and close the dialog; null when no window on this desktop takes a draft. */
  readonly onMakeTheme: (() => void) | null;
  /** The app whose icon pictures each theme, as that theme draws it (null when absent). */
  readonly previewApp: Pick<AppRecord, "name" | "icon"> | null;
  /** Show a picked theme (a theme id, or null for the workspace's default) before it is saved. */
  readonly onPreviewTheme: (choice: string | null) => void;
  /** Whether the desktop is the one on screen, where a picked theme shows before it is saved. */
  readonly isDesktopShown: boolean;
  /** The dialog closed: put back whatever the desktop has saved by then. */
  readonly onClearPreview: () => void;
}

/** What the chooser saves: the desktop's own theme (null wears the workspace's default), and whether that theme
 *  becomes the workspace's default instead, worn by every desktop without a theme of its own. */
export interface ThemeChoice {
  readonly theme: string | null;
  readonly isWorkspaceDefault: boolean;
}

export const THEME_PREVIEW_APP = "files";

/** The prompt "Make your own..." drafts, unsent, into the pinned window's chat. */
export const THEME_DESIGN_PROMPT =
  "I'd like to make my own workspace theme. Design it, show me how it looks, and let me try it " +
  "in the theme chooser before anything changes. Here's the look I want: ";

/** Under the tiles of a desktop that is not on screen, where a picked theme cannot show before it is saved. */
export const THEME_PREVIEW_ELSEWHERE_HINT = "Switch to this desktop to see a theme before you save it.";

/** Where "Make your own..." would be, when no window on this desktop takes a draft. */
export const MAKE_THEME_IN_CHAT_HINT = "To make your own theme, describe the look you want in a chat.";

export function ThemeChooserDialog(): m.Component<ThemeChooserDialogAttrs> {
  let theme: string | null = null;
  let isWorkspaceDefault = false;
  let isSaving = false;
  let error: string | null = null;

  async function save(attrs: ThemeChooserDialogAttrs): Promise<void> {
    if (isSaving) return;
    isSaving = true;
    error = null;
    m.redraw();
    try {
      await attrs.onSave({
        theme,
        isWorkspaceDefault:
          isWorkspaceDefault && attrs.themes !== null && availableTheme(attrs.themes, theme) !== null,
      });
    } catch (e) {
      error = (e as Error).message;
      isSaving = false;
    }
    m.redraw();
  }

  /** A theme's picture in its tile: its own Files icon, else its generic program icon (an icon a theme
   *  derives is drawn in the page only for the theme being worn); Files' own icon for a theme that draws
   *  none (the standard look); and a generic glyph for an unavailable theme, whose files are not served,
   *  or when there is no Files to show. */
  function themePicture(record: ThemeRecord, previewApp: Pick<AppRecord, "name" | "icon"> | null): m.Children {
    const icons = record.available ? record.icons : null;
    const url = icons === null ? null : (icons.apps[THEME_PREVIEW_APP] ?? icons.fallback_url);
    if (record.available && url === null && previewApp !== null) {
      return m(
        "span",
        { "data-theme-picture": "standard", class: "flex h-8 w-8 items-center justify-center" },
        m.trust(appIconMarkupForApp(previewApp, 32, genericGlyph("app", 32))),
      );
    }
    if (url === null || icons === null) {
      return m(
        "span",
        { class: "flex h-8 w-8 items-center justify-center text-secondary" },
        m.trust(genericGlyph("app", 28)),
      );
    }
    return m("img", {
      class: "h-8 w-8",
      src: url,
      alt: "",
      style: icons.rendering === "pixelated" ? "image-rendering: pixelated" : undefined,
    });
  }

  /** One choice: a theme, or (`value` null) the workspace's default, named after the theme it is. */
  function themeTile(
    attrs: ThemeChooserDialogAttrs,
    value: string | null,
    label: string,
    record: ThemeRecord,
    unavailableReason: string | null,
  ): m.Vnode {
    const isSelected = value === theme;
    const isDisabled = unavailableReason !== null;
    return m(
      "button",
      {
        type: "button",
        "data-theme": value ?? "workspace-default",
        ...TILE_PART,
        class:
          "flex w-28 shrink-0 flex-col items-center gap-1 rounded-md border bg-transparent p-2 " +
          (isDisabled ? "cursor-not-allowed opacity-50 " : "cursor-pointer ") +
          (isSelected ? "border-accent ring-2 ring-accent" : "border-default"),
        "aria-pressed": isSelected ? "true" : "false",
        "aria-disabled": isDisabled ? "true" : undefined,
        title: unavailableReason ?? undefined,
        onclick() {
          if (isDisabled) return;
          theme = value;
          attrs.onPreviewTheme(theme);
        },
      },
      [themePicture(record, attrs.previewApp), m("span", { class: "type-helper text-center text-primary" }, label)],
    );
  }

  /** "Make your own...": drafts the theme prompt into the chat, where the agent makes the theme. */
  function makeThemeTile(attrs: ThemeChooserDialogAttrs): m.Children {
    const onMakeTheme = attrs.onMakeTheme;
    if (onMakeTheme === null) return null;
    return m(
      "button",
      {
        type: "button",
        "data-make-theme": "",
        ...TILE_PART,
        class:
          "flex w-28 shrink-0 cursor-pointer flex-col items-center gap-1 rounded-md border border-dashed " +
          "border-default bg-transparent p-2",
        onclick: onMakeTheme,
      },
      [
        m(
          "span",
          { "aria-hidden": "true", class: "flex h-8 w-8 items-center justify-center text-2xl text-secondary" },
          "+",
        ),
        m("span", { class: "type-helper text-center text-primary" }, "Make your own..."),
      ],
    );
  }

  /** A saved choice that is no longer available (deleted, or failing its checks) is worn as the standard look. */
  function unavailableChoiceNote(catalog: ThemeCatalog): m.Children {
    if (theme === null || availableTheme(catalog, theme) !== null) return null;
    const chosen = theme;
    const name = catalog.themes.find((record) => record.id === chosen)?.name ?? chosen;
    return m(
      "p",
      { "data-theme-unavailable": "", class: "mb-2 type-helper text-secondary" },
      `This desktop's theme, ${name}, is not available, so it wears the standard look.`,
    );
  }

  function themeRow(attrs: ThemeChooserDialogAttrs): m.Children {
    const catalog = attrs.themes;
    if (catalog === null) {
      return m("p", { "data-themes-loading": "", class: "mb-3 type-helper text-faint" }, "Loading themes…");
    }
    const defaultTheme = resolveDesktopTheme(catalog, null);
    const tiles = [
      themeTile(attrs, null, `Workspace default (${defaultTheme.name})`, defaultTheme, null),
      ...catalog.themes.map((record) =>
        themeTile(
          attrs,
          record.id,
          record.name,
          record,
          record.available ? null : `Unavailable: ${record.problems.join("; ")}`,
        ),
      ),
    ];
    return [
      m("div", { class: "mb-2 flex flex-wrap gap-2" }, [...tiles, makeThemeTile(attrs)]),
      attrs.onMakeTheme === null
        ? m("p", { "data-make-theme-hint": "", class: "mb-2 type-helper text-secondary" }, MAKE_THEME_IN_CHAT_HINT)
        : null,
      attrs.isDesktopShown
        ? null
        : m(
            "p",
            { "data-theme-preview-hint": "", class: "mb-2 type-helper text-secondary" },
            THEME_PREVIEW_ELSEWHERE_HINT,
          ),
      unavailableChoiceNote(catalog),
      availableTheme(catalog, theme) === null
        ? null
        : m("label", { class: "mb-3 flex items-center gap-2 type-helper text-primary" }, [
            m("input", {
              type: "checkbox",
              "data-theme-as-workspace-default": "",
              checked: isWorkspaceDefault,
              onchange(event: Event) {
                isWorkspaceDefault = (event.target as HTMLInputElement).checked;
              },
            }),
            "Make this the workspace default",
          ]),
    ];
  }

  return {
    oninit(vnode) {
      theme = vnode.attrs.desktop.theme;
    },
    onremove(vnode) {
      vnode.attrs.onClearPreview();
    },
    view(vnode) {
      const attrs = vnode.attrs;
      return m(
        Modal,
        {
          onDismiss: attrs.onCancel,
          onEscape: attrs.onCancel,
          title: "Change theme",
          card: { "data-theme-chooser": attrs.desktop.id },
          actions: [
            m(Button, { onclick: attrs.onCancel, disabled: isSaving }, "Cancel"),
            m(
              Button,
              {
                variant: "primary",
                extra: "theme-chooser-save",
                onclick: () => save(attrs),
                disabled: isSaving,
              },
              isSaving ? "Saving..." : "Save",
            ),
          ],
        },
        [
          m("label", { class: MODAL_LABEL_CLASS }, `Theme of ${attrs.desktop.name}`),
          themeRow(attrs),
          error ? m("p", { class: "type-helper mt-1 text-danger" }, error) : null,
        ],
      );
    },
  };
}
