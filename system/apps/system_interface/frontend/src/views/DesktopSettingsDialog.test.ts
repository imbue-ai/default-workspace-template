// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "../testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { desktopRecord } from "../testing/records";
import { DesktopSettingsDialog, isSameWallpaper } from "./DesktopSettingsDialog";
import type { DesktopSettingsDialogAttrs } from "./DesktopSettingsDialog";

afterEach(unmountViews);

function render(overrides: Partial<DesktopSettingsDialogAttrs> = {}): DesktopSettingsDialogAttrs {
  const attrs: DesktopSettingsDialogAttrs = {
    desktop: desktopRecord("home", { name: "Home" }),
    wallpapers: [],
    isDeleting: false,
    onSave: vi.fn(async () => undefined),
    onDelete: vi.fn(async () => undefined),
    onCancel: vi.fn(),
    ...overrides,
  };
  mountView(() => m(DesktopSettingsDialog, attrs));
  return attrs;
}

function card(): HTMLElement {
  return document.querySelector("[data-desktop-settings]") as HTMLElement;
}

function pressEnterInNameField(): void {
  const input = card().querySelector(".desktop-settings-name") as HTMLInputElement;
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  m.redraw.sync();
}

function pressEscape(): void {
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  m.redraw.sync();
}

/** Let the save or delete promise settle and the view take its outcome. */
async function settled(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  m.redraw.sync();
}

describe("DesktopSettingsDialog", () => {
  it("saves the desktop's settings on Enter in the name field", async () => {
    const attrs = render();
    pressEnterInNameField();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith("Home", expect.any(String), expect.any(Number), "shared", null);
  });

  it("does not save on Enter while the delete confirmation is showing", async () => {
    const attrs = render({ isDeleting: true });
    expect(card().querySelector(".desktop-settings-confirm-delete")).not.toBeNull();
    pressEnterInNameField();
    await settled();
    expect(attrs.onSave).not.toHaveBeenCalled();
  });

  it("shows the shell's refusal of a save and lets the user try again", async () => {
    const attrs = render({ onSave: vi.fn(async () => Promise.reject(new Error("that name is taken"))) });
    pressEnterInNameField();
    await settled();
    expect(card().textContent).toContain("that name is taken");
    expect((card().querySelector(".desktop-settings-save") as HTMLButtonElement).disabled).toBe(false);
    expect(attrs.onSave).toHaveBeenCalledTimes(1);
  });

  it("Escape drops the delete confirmation first, and cancels the dialog only after", () => {
    const attrs = render({ isDeleting: true });
    pressEscape();
    expect(card().querySelector(".desktop-settings-confirm-delete")).toBeNull();
    expect(card().querySelector(".desktop-settings-save")).not.toBeNull();
    expect(attrs.onCancel).not.toHaveBeenCalled();
    pressEscape();
    expect(attrs.onCancel).toHaveBeenCalledTimes(1);
  });

  it("confirming the delete asks for it once", async () => {
    const attrs = render();
    (card().querySelector(".desktop-settings-delete") as HTMLButtonElement).click();
    m.redraw.sync();
    (card().querySelector(".desktop-settings-confirm-delete") as HTMLButtonElement).click();
    await settled();
    expect(attrs.onDelete).toHaveBeenCalledTimes(1);
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
