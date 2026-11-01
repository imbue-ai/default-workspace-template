// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { desktopRecord } from "../testing/records";
import { WallpaperChooserDialog, isSameWallpaper } from "./WallpaperChooserDialog";
import type { WallpaperChooserDialogAttrs } from "./WallpaperChooserDialog";

afterEach(unmountViews);

const ARCS = { kind: "bundled" as const, name: "arcs", url: "/wallpapers/bundled/arcs" };
const DAWN = { kind: "bundled" as const, name: "dawn", url: "/wallpapers/bundled/dawn" };

function render(overrides: Partial<WallpaperChooserDialogAttrs> = {}): WallpaperChooserDialogAttrs {
  const attrs: WallpaperChooserDialogAttrs = {
    desktop: desktopRecord("home", { name: "Home" }),
    wallpapers: [ARCS, DAWN],
    onSave: vi.fn(async () => undefined),
    onCancel: vi.fn(),
    ...overrides,
  };
  mountView(() => m(WallpaperChooserDialog, attrs));
  return attrs;
}

function card(): HTMLElement {
  return document.querySelector("[data-wallpaper-chooser]") as HTMLElement;
}

function save(): void {
  (card().querySelector(".wallpaper-chooser-save") as HTMLButtonElement).click();
}

function pressed(): string[] {
  return [...card().querySelectorAll<HTMLButtonElement>("[data-wallpaper]")]
    .filter((swatch) => swatch.getAttribute("aria-pressed") === "true")
    .map((swatch) => swatch.getAttribute("data-wallpaper") as string);
}

/** Let the save promise settle and the view take its outcome. */
async function settled(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  m.redraw.sync();
}

describe("WallpaperChooserDialog", () => {
  it("names the desktop, offers the default and every wallpaper, and saves the picked one", async () => {
    const attrs = render();
    expect(card().textContent).toContain("Wallpaper of Home");
    expect(
      [...card().querySelectorAll("[data-wallpaper]")].map((swatch) => swatch.getAttribute("data-wallpaper")),
    ).toEqual(["default", "bundled:arcs", "bundled:dawn"]);
    expect(pressed()).toEqual(["default"]);
    (card().querySelector('[data-wallpaper="bundled:dawn"]') as HTMLButtonElement).click();
    m.redraw.sync();
    expect(pressed()).toEqual(["bundled:dawn"]);
    save();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith({ kind: "bundled", name: "dawn" });
  });

  it("shows the shell's refusal of a save and lets the user try again", async () => {
    const attrs = render({ onSave: vi.fn(async () => Promise.reject(new Error("no such wallpaper"))) });
    save();
    await settled();
    expect(card().textContent).toContain("no such wallpaper");
    expect((card().querySelector(".wallpaper-chooser-save") as HTMLButtonElement).disabled).toBe(false);
    expect(attrs.onSave).toHaveBeenCalledTimes(1);
  });

  it("Escape cancels", () => {
    const attrs = render();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    m.redraw.sync();
    expect(attrs.onCancel).toHaveBeenCalledTimes(1);
  });

  it("reads a wallpaper the workspace no longer offers as Default and saves it as such, clearing the dead reference", async () => {
    const attrs = render({
      desktop: desktopRecord("home", { name: "Home", wallpaper: { kind: "bundled", name: "retired" } }),
      wallpapers: [ARCS],
    });
    expect(pressed()).toEqual(["default"]);
    save();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith(null);
  });

  it("leaves a wallpaper that is still on offer selected and saved as itself", async () => {
    const attrs = render({
      desktop: desktopRecord("home", { name: "Home", wallpaper: { kind: "bundled", name: "arcs" } }),
      wallpapers: [ARCS],
    });
    expect(pressed()).toEqual(["bundled:arcs"]);
    save();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith({ kind: "bundled", name: "arcs" });
  });
});

describe("isSameWallpaper", () => {
  it("compares kind and name, and reads two defaults as the same", () => {
    expect(isSameWallpaper(null, null)).toBe(true);
    expect(isSameWallpaper({ kind: "bundled", name: "dawn" }, null)).toBe(false);
    expect(isSameWallpaper({ kind: "bundled", name: "dawn" }, { kind: "file", name: "dawn" })).toBe(false);
    expect(isSameWallpaper({ kind: "bundled", name: "dawn" }, { kind: "bundled", name: "dawn" })).toBe(true);
  });
});
