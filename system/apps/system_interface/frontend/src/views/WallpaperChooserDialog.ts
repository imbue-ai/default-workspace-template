/**
 * The wallpaper chooser (the multiplayer spec, section 8.2): what the desktop settings dialog became once a
 * desktop's name moved to the overview and its colour and glyph went away. Opened from "Change wallpaper..." on the
 * backdrop's context menu, it offers the bundled wallpapers and the files in the workspace's wallpapers directory,
 * and the theme's default.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { MODAL_LABEL_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import type { Desktop, Wallpaper, WallpaperListing } from "../model/records";

export interface WallpaperChooserDialogAttrs {
  readonly desktop: Desktop;
  /** The wallpapers on offer, bundled first; null while they load. */
  readonly wallpapers: readonly WallpaperListing[] | null;
  readonly onSave: (wallpaper: Wallpaper | null) => Promise<void>;
  readonly onCancel: () => void;
}

export function isSameWallpaper(first: Wallpaper | null, second: Wallpaper | null): boolean {
  if (first === null || second === null) return first === second;
  return first.kind === second.kind && first.name === second.name;
}

export function WallpaperChooserDialog(): m.Component<WallpaperChooserDialogAttrs> {
  let wallpaper: Wallpaper | null = null;
  let isSaving = false;
  let error: string | null = null;

  async function save(attrs: WallpaperChooserDialogAttrs): Promise<void> {
    if (isSaving) return;
    isSaving = true;
    error = null;
    m.redraw();
    try {
      await attrs.onSave(resolvedWallpaper(attrs.wallpapers));
    } catch (e) {
      error = (e as Error).message;
      isSaving = false;
    }
    m.redraw();
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

  function wallpaperPicker(attrs: WallpaperChooserDialogAttrs): m.Children {
    if (attrs.wallpapers === null) return m("p", { class: "type-helper text-faint" }, "Loading wallpapers…");
    const selected = resolvedWallpaper(attrs.wallpapers);
    return m("div", { class: "flex flex-wrap gap-2" }, [
      wallpaperChoice("Default", null, null, selected),
      attrs.wallpapers.map((listing) =>
        wallpaperChoice(listing.name, { kind: listing.kind, name: listing.name }, listing.url, selected),
      ),
    ]);
  }

  return {
    oninit(vnode) {
      wallpaper = vnode.attrs.desktop.wallpaper;
    },
    view(vnode) {
      const attrs = vnode.attrs;
      return m(
        Modal,
        {
          onDismiss: attrs.onCancel,
          onEscape: attrs.onCancel,
          title: "Change wallpaper",
          card: { "data-wallpaper-chooser": attrs.desktop.id },
          actions: [
            m(Button, { onclick: attrs.onCancel, disabled: isSaving }, "Cancel"),
            m(
              Button,
              {
                variant: "primary",
                extra: "wallpaper-chooser-save",
                onclick: () => save(attrs),
                disabled: isSaving,
              },
              isSaving ? "Saving..." : "Save",
            ),
          ],
        },
        [
          m("label", { class: MODAL_LABEL_CLASS }, `Wallpaper of ${attrs.desktop.name}`),
          m("div", { class: "mb-3" }, wallpaperPicker(attrs)),
          error ? m("p", { class: "type-helper mt-1 text-danger" }, error) : null,
        ],
      );
    },
  };
}
