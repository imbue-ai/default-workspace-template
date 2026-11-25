// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { refusedWith, wordFailureAsTheStoreDoes } from "../testing/failures";
import { desktopRecord } from "../testing/records";
import { WallpaperChooserDialog, isSameWallpaper } from "./WallpaperChooserDialog";
import type { WallpaperChooserDialogAttrs } from "./WallpaperChooserDialog";

afterEach(unmountViews);

const reportBug = vi.fn();

const ARCS = { kind: "bundled" as const, name: "arcs", url: "/wallpapers/bundled/arcs" };
const DAWN = { kind: "bundled" as const, name: "dawn", url: "/wallpapers/bundled/dawn" };

function render(overrides: Partial<WallpaperChooserDialogAttrs> = {}): WallpaperChooserDialogAttrs {
  const attrs: WallpaperChooserDialogAttrs = {
    desktop: desktopRecord("home", { name: "Home" }),
    wallpapers: [ARCS, DAWN],
    wordFailure: wordFailureAsTheStoreDoes,
    onReportBug: reportBug,
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

  it("says the wallpaper couldn't be changed, with the shell's reason behind See details, and lets the user try again", async () => {
    const attrs = render({
      onSave: vi.fn(async () => Promise.reject(refusedWith(400, { detail: "no such wallpaper" }))),
    });
    save();
    await settled();
    expect(card().querySelector(".wallpaper-chooser-error-message")?.textContent).toBe(
      "The wallpaper couldn't be changed.",
    );
    expect(card().querySelector(".see-details pre")?.textContent).toBe("HTTP 400\nno such wallpaper");
    expect((card().querySelector(".wallpaper-chooser-save") as HTMLButtonElement).disabled).toBe(false);
    expect(attrs.onSave).toHaveBeenCalledTimes(1);
  });

  it("offers Try again, which saves again, and Report a bug about the line's failure, for a save with no reason", async () => {
    const attrs = render({ onSave: vi.fn(async () => Promise.reject(refusedWith(500, null, "boom"))) });
    save();
    await settled();
    const message = "The wallpaper couldn't be changed. Try again.";
    expect(card().querySelector(".wallpaper-chooser-error-message")?.textContent).toBe(message);
    const buttons = [...card().querySelectorAll<HTMLButtonElement>(".wallpaper-chooser-error-action")];
    expect(buttons.map((button) => button.textContent)).toEqual(["Try again", "Report a bug"]);
    buttons[1].click();
    expect(reportBug).toHaveBeenCalledWith({ message, details: "HTTP 500\nboom" });
    buttons[0].click();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledTimes(2);
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
