/**
 * The dialog for one desktop's settings (plan section 4.8): its name, colour, glyph, wallpaper, and theme,
 * plus the one place a desktop can be deleted. Deleting is confirm-gated in
 * place (a second, red button inside this same dialog) rather than a second stacked dialog. The
 * last desktop cannot be deleted; the shell refuses with a 409 the dialog shows.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { MODAL_LABEL_CLASS, MODAL_MESSAGE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import type { Desktop, Wallpaper, WallpaperListing } from "../model/records";
import { resolveDesktopTheme, type ThemeCatalog, type ThemeRecord } from "../model/themes";
import { SQUIGGLE_GLYPHS, squiggleMarkup } from "./squiggles";
import { FIELD_PART, TILE_PART } from "@imbue/workspace-ui/src/themes/parts";

// The palette is exactly the glyphs' own signature colors, so every desktop colour belongs to
// the family the squiggles were drawn in.
const PALETTE: readonly string[] = SQUIGGLE_GLYPHS.map((glyph) => glyph.color);

const PREVIEW_GLYPH_SIZE = 40;
const PICKER_GLYPH_SIZE = 28;

export interface DesktopSettingsDialogAttrs {
  readonly desktop: Desktop;
  /** The wallpapers on offer, bundled first; null while they load. */
  readonly wallpapers: readonly WallpaperListing[] | null;
  /** Whether the dialog opens straight into the delete confirmation. */
  readonly isDeleting: boolean;
  readonly onSave: (
    name: string,
    color: string,
    glyph: number,
    wallpaper: Wallpaper | null,
    themeChoice: ThemeChoice,
  ) => Promise<void>;
  readonly onDelete: () => Promise<void>;
  readonly onCancel: () => void;
  /** The workspace's themes, for the Theme row. */
  readonly themes: ThemeCatalog;
  /** Show a picked theme (a theme id, or null for the workspace's default) before it is saved. */
  readonly onPreviewTheme: (choice: string | null) => void;
  /** The dialog closed: put back whatever the desktop has saved by then. */
  readonly onClearPreview: () => void;
}

/** What the Theme row saves: the desktop's own theme (null wears the workspace's default), and whether that theme
 *  becomes the workspace's default instead, worn by every desktop without a theme of its own. */
export interface ThemeChoice {
  readonly theme: string | null;
  readonly isWorkspaceDefault: boolean;
}

const THEME_PREVIEW_APP = "files";

function normalizedGlyphIndex(glyph: number): number {
  const count = SQUIGGLE_GLYPHS.length;
  return ((Math.trunc(glyph) % count) + count) % count;
}

export function isSameWallpaper(first: Wallpaper | null, second: Wallpaper | null): boolean {
  if (first === null || second === null) return first === second;
  return first.kind === second.kind && first.name === second.name;
}

export function DesktopSettingsDialog(): m.Component<DesktopSettingsDialogAttrs> {
  let name = "";
  let color = SQUIGGLE_GLYPHS[0].color;
  let glyphIndex = 0;
  let wallpaper: Wallpaper | null = null;
  let theme: string | null = null;
  let isWorkspaceDefault = false;
  let isSaving = false;
  let isDeleting = false;
  let isConfirmingDelete = false;
  let error: string | null = null;

  async function save(attrs: DesktopSettingsDialogAttrs): Promise<void> {
    const chosen = name.trim();
    // Not while the delete confirmation is up: the name field is still there (and focused) under it.
    if (!chosen || isSaving || isDeleting || isConfirmingDelete) return;
    isSaving = true;
    error = null;
    m.redraw();
    try {
      await attrs.onSave(chosen, color, glyphIndex, resolvedWallpaper(attrs.wallpapers), {
        theme,
        isWorkspaceDefault: isWorkspaceDefault && theme !== null,
      });
    } catch (e) {
      error = (e as Error).message;
      isSaving = false;
    }
    m.redraw();
  }

  async function deleteDesktop(attrs: DesktopSettingsDialogAttrs): Promise<void> {
    if (isSaving || isDeleting) return;
    isDeleting = true;
    error = null;
    m.redraw();
    try {
      await attrs.onDelete();
    } catch (e) {
      error = (e as Error).message;
      isDeleting = false;
    }
    m.redraw();
  }

  function colorSwatch(swatch: string): m.Vnode {
    const isSelected = swatch === color;
    return m("button", {
      type: "button",
      class:
        "h-6 w-6 cursor-pointer rounded-full border-0 p-0 outline-offset-2 " +
        (isSelected ? "outline-2 outline-primary" : "outline-none"),
      style: { background: swatch },
      "aria-label": `Color ${swatch}`,
      "aria-pressed": isSelected ? "true" : "false",
      onclick() {
        color = swatch;
      },
    });
  }

  function glyphCell(index: number): m.Vnode {
    const isSelected = index === glyphIndex;
    return m(
      "button",
      {
        type: "button",
        // The selection ring is a ring rather than a thicker border, so picking a glyph never nudges the grid.
        class:
          "flex h-12 cursor-pointer items-center justify-center rounded-md border bg-transparent " +
          (isSelected ? "ring-1" : "border-default"),
        style: isSelected ? { borderColor: color, "--tw-ring-color": color } : {},
        "aria-label": `Squiggle ${index + 1}`,
        "aria-pressed": isSelected ? "true" : "false",
        onclick() {
          glyphIndex = index;
        },
      },
      m.trust(squiggleMarkup(index, color, PICKER_GLYPH_SIZE)),
    );
  }

  /**
   * The held reference, or null when the wallpapers on offer do not have it.
   *
   * A reference outlives the file it names -- a bundled wallpaper the shell stopped shipping, a
   * file taken back out of the wallpapers directory -- and the backdrop already draws the default
   * in its place. The picker says the same thing rather than leaving every swatch unlit, and a
   * save writes what the picker showed, so agreeing with the backdrop also clears the dead
   * reference. While the listings are still loading there is nothing to check it against, so the
   * reference stands.
   */
  function resolvedWallpaper(listings: readonly WallpaperListing[] | null): Wallpaper | null {
    if (wallpaper === null || listings === null) return wallpaper;
    return listings.some((listing) => isSameWallpaper({ kind: listing.kind, name: listing.name }, wallpaper))
      ? wallpaper
      : null;
  }

  function wallpaperChoice(
    label: string,
    value: Wallpaper | null,
    url: string | null,
    selected: Wallpaper | null,
  ): m.Vnode {
    const isSelected = isSameWallpaper(value, selected);
    return m(
      "button",
      {
        type: "button",
        "data-wallpaper": value === null ? "default" : `${value.kind}:${value.name}`,
        class:
          "flex h-14 w-20 shrink-0 cursor-pointer flex-col items-center justify-end overflow-hidden rounded-md border bg-cover bg-center " +
          (isSelected ? "border-accent ring-2 ring-accent" : "border-default"),
        style: url === null ? "" : `background-image: url("${url}")`,
        "aria-pressed": isSelected ? "true" : "false",
        onclick() {
          wallpaper = value;
        },
      },
      m("span", { class: "w-full truncate bg-surface/80 px-1 text-center type-helper text-primary" }, label),
    );
  }

  function wallpaperPicker(attrs: DesktopSettingsDialogAttrs): m.Children {
    if (attrs.wallpapers === null) return m("p", { class: "type-helper text-faint" }, "Loading wallpapers…");
    const selected = resolvedWallpaper(attrs.wallpapers);
    return m("div", { class: "flex flex-wrap gap-2" }, [
      wallpaperChoice("Default", null, null, selected),
      attrs.wallpapers.map((listing) =>
        wallpaperChoice(listing.name, { kind: listing.kind, name: listing.name }, listing.url, selected),
      ),
    ]);
  }

  /** A theme's picture in its tile: the icon it draws for the File Viewer, or the desktop's own squiggle for a
   *  theme without pixel icons (the standard look) and for an unavailable one, whose files are not served. */
  function themePicture(record: ThemeRecord): m.Children {
    const icons = record.available ? record.icons : null;
    const url = icons === null ? null : (icons.apps[THEME_PREVIEW_APP] ?? icons.fallback_url);
    if (url === null || icons === null) {
      return m(
        "span",
        { class: "flex h-8 w-8 items-center justify-center" },
        m.trust(squiggleMarkup(glyphIndex, color, 28)),
      );
    }
    return m("img", {
      class: "h-8 w-8",
      src: url,
      alt: "",
      style: icons.rendering === "pixelated" ? "image-rendering: pixelated" : undefined,
    });
  }

  /** One choice in the Theme row: a theme, or (`value` null) the workspace's default, named after the theme it is. */
  function themeTile(
    attrs: DesktopSettingsDialogAttrs,
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
      [themePicture(record), m("span", { class: "type-helper text-center text-primary" }, label)],
    );
  }

  function themeRow(attrs: DesktopSettingsDialogAttrs): m.Children {
    const catalog = attrs.themes;
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
      m("div", { class: "mb-2 flex flex-wrap gap-2" }, tiles),
      theme === null
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

  function deleteConfirmationActions(attrs: DesktopSettingsDialogAttrs): m.Children {
    return [
      m(
        Button,
        {
          disabled: isDeleting,
          onclick() {
            isConfirmingDelete = false;
          },
        },
        "Keep desktop",
      ),
      m(
        Button,
        {
          variant: "destructive",
          extra: "desktop-settings-confirm-delete",
          disabled: isDeleting,
          onclick: () => deleteDesktop(attrs),
        },
        isDeleting ? "Deleting..." : "Delete desktop",
      ),
    ];
  }

  function editActions(attrs: DesktopSettingsDialogAttrs, trimmedName: string): m.Children {
    return [
      m(
        Button,
        {
          extra: "desktop-settings-delete mr-auto",
          disabled: isSaving || isDeleting,
          onclick() {
            isConfirmingDelete = true;
          },
        },
        "Delete",
      ),
      m(Button, { onclick: attrs.onCancel, disabled: isSaving || isDeleting }, "Cancel"),
      m(
        Button,
        {
          variant: "primary",
          extra: "desktop-settings-save",
          onclick: () => save(attrs),
          disabled: isSaving || isDeleting || !trimmedName,
        },
        isSaving ? "Saving..." : "Save",
      ),
    ];
  }

  return {
    oninit(vnode) {
      const { desktop } = vnode.attrs;
      name = desktop.name;
      color = desktop.color;
      glyphIndex = normalizedGlyphIndex(desktop.glyph);
      wallpaper = desktop.wallpaper;
      theme = desktop.theme;
      isConfirmingDelete = vnode.attrs.isDeleting;
    },
    onremove(vnode) {
      vnode.attrs.onClearPreview();
    },
    view(vnode) {
      const attrs = vnode.attrs;
      const trimmedName = name.trim();
      return m(
        Modal,
        {
          onDismiss: attrs.onCancel,
          onEscape: () => {
            if (isConfirmingDelete) isConfirmingDelete = false;
            else attrs.onCancel();
          },
          title: "Desktop settings",
          card: { "data-desktop-settings": attrs.desktop.id },
          actions: isConfirmingDelete ? deleteConfirmationActions(attrs) : editActions(attrs, trimmedName),
        },
        [
          m("div", { class: "mb-4 flex items-center gap-3" }, [
            m(
              "span",
              { class: "flex h-10 w-10 shrink-0 items-center justify-center" },
              m.trust(squiggleMarkup(glyphIndex, color, PREVIEW_GLYPH_SIZE)),
            ),
            m("span", { class: "truncate type-label text-primary" }, trimmedName || "Untitled desktop"),
          ]),
          m("label", { class: MODAL_LABEL_CLASS }, "Name"),
          m("input", {
            class: inputClass({ extra: "mb-3 desktop-settings-name" }),
            ...FIELD_PART,
            type: "text",
            value: name,
            placeholder: "desktop name",
            disabled: isSaving || isDeleting,
            oncreate: (created: m.VnodeDOM) => (created.dom as HTMLInputElement).focus(),
            oninput(event: InputEvent) {
              name = (event.target as HTMLInputElement).value;
            },
            onkeydown(event: KeyboardEvent) {
              if (event.key === "Enter") void save(attrs);
            },
          }),
          m("label", { class: MODAL_LABEL_CLASS }, "Color"),
          m("div", { class: "mb-3 flex flex-wrap gap-2" }, PALETTE.map(colorSwatch)),
          m("label", { class: MODAL_LABEL_CLASS }, "Squiggle"),
          m(
            "div",
            { class: "mb-3 grid grid-cols-5 gap-2" },
            SQUIGGLE_GLYPHS.map((_glyph, index) => glyphCell(index)),
          ),
          m("label", { class: MODAL_LABEL_CLASS }, "Wallpaper"),
          m("div", { class: "mb-3" }, wallpaperPicker(attrs)),
          m("label", { class: MODAL_LABEL_CLASS }, "Theme"),
          themeRow(attrs),
          error ? m("p", { class: "type-helper mt-1 text-danger" }, error) : null,
          isConfirmingDelete
            ? m("p", { class: MODAL_MESSAGE_CLASS }, [
                "Delete ",
                m("strong", attrs.desktop.name),
                "? Its windows close for everyone.",
              ])
            : null,
        ],
      );
    },
  };
}
