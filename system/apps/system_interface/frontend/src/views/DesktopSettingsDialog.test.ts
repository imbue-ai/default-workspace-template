// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, desktopRecord, themeCatalog, themeRecord } from "../testing/records";
import { DesktopSettingsDialog, MAKE_THEME_IN_CHAT_HINT, isSameWallpaper } from "./DesktopSettingsDialog";
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
    onPreviewTheme: vi.fn(),
    onClearPreview: vi.fn(),
    previewApp: null,
    onMakeTheme: null,
    themes: themeCatalog(
      themeRecord("mac-classic", { name: "Classic Mac" }),
      themeRecord("windows-2000", { name: "Windows 2000" }),
      themeRecord("broken", { available: false, problems: ["parts.css:1: .x is a class"] }),
    ),
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
    expect(attrs.onSave).toHaveBeenCalledWith("Home", expect.any(String), expect.any(Number), null, {
      theme: null,
      isWorkspaceDefault: false,
    });
  });

  it("saves every edited field: the typed name, the picked colour, glyph, wallpaper, and theme", async () => {
    const attrs = render({ wallpapers: [{ kind: "bundled", name: "dawn", url: "/wallpapers/bundled/dawn" }] });
    const input = card().querySelector(".desktop-settings-name") as HTMLInputElement;
    input.value = "  Studio ";
    input.dispatchEvent(new InputEvent("input", { bubbles: true }));
    const swatches = card().querySelectorAll<HTMLButtonElement>('[aria-label^="Color "]');
    swatches[swatches.length - 1].click();
    (card().querySelector('[aria-label="Squiggle 3"]') as HTMLButtonElement).click();
    (card().querySelector('[data-wallpaper="bundled:dawn"]') as HTMLButtonElement).click();
    (card().querySelector('[data-theme="windows-2000"]') as HTMLButtonElement).click();
    (card().querySelector(".desktop-settings-save") as HTMLButtonElement).click();
    await settled();
    const pickedColor = swatches[swatches.length - 1].getAttribute("aria-label")?.replace("Color ", "");
    expect(attrs.onSave).toHaveBeenCalledWith(
      "Studio",
      pickedColor,
      2,
      { kind: "bundled", name: "dawn" },
      { theme: "windows-2000", isWorkspaceDefault: false },
    );
  });

  it("previews a picked theme at once and drops the preview when the dialog closes", () => {
    const attrs = render();
    (card().querySelector('[data-theme="mac-classic"]') as HTMLButtonElement).click();
    expect(attrs.onPreviewTheme).toHaveBeenLastCalledWith("mac-classic");
    expect(attrs.onSave).not.toHaveBeenCalled();
    unmountViews();
    expect(attrs.onClearPreview).toHaveBeenCalledOnce();
  });

  it("lists the workspace default by the theme it is, and a theme that fails its checks as unavailable", () => {
    render({
      themes: { ...themeCatalog(themeRecord("mac-classic", { name: "Classic Mac" })), default: "mac-classic" },
    });
    const defaultTile = card().querySelector('[data-theme="workspace-default"]') as HTMLButtonElement;
    expect(defaultTile.textContent).toContain("Workspace default (Classic Mac)");
    expect(defaultTile.getAttribute("aria-pressed")).toBe("true");
    const brokenAttrs = render({
      themes: themeCatalog(themeRecord("broken", { available: false, problems: ["no icons"] })),
    });
    const broken = document.querySelectorAll('[data-theme="broken"]');
    const brokenTile = broken[broken.length - 1] as HTMLButtonElement;
    expect(brokenTile.getAttribute("aria-disabled")).toBe("true");
    expect(brokenTile.title).toBe("Unavailable: no icons");
    brokenTile.click();
    expect(brokenAttrs.onPreviewTheme).not.toHaveBeenCalled();
  });

  it("pictures an unavailable theme without loading its icons, which are not served", () => {
    render({ themes: themeCatalog(themeRecord("broken", { available: false, problems: ["no icons"] })) });
    const brokenTile = card().querySelector('[data-theme="broken"]') as HTMLButtonElement;
    expect(brokenTile.querySelector("img")).toBeNull();
    expect(brokenTile.querySelector("svg")).not.toBeNull();
  });

  it("makes a picked theme the workspace default when that box is checked", async () => {
    const attrs = render();
    (card().querySelector('[data-theme="mac-classic"]') as HTMLButtonElement).click();
    m.redraw.sync();
    const asDefault = card().querySelector("[data-theme-as-workspace-default]") as HTMLInputElement;
    asDefault.checked = true;
    asDefault.dispatchEvent(new Event("change", { bubbles: true }));
    (card().querySelector(".desktop-settings-save") as HTMLButtonElement).click();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith("Home", expect.any(String), 0, null, {
      theme: "mac-classic",
      isWorkspaceDefault: true,
    });
  });

  it("does not save a blank name: Save is disabled and Enter posts nothing", async () => {
    const attrs = render();
    const input = card().querySelector(".desktop-settings-name") as HTMLInputElement;
    input.value = "   ";
    input.dispatchEvent(new InputEvent("input", { bubbles: true }));
    m.redraw.sync();
    expect((card().querySelector(".desktop-settings-save") as HTMLButtonElement).disabled).toBe(true);
    pressEnterInNameField();
    await settled();
    expect(attrs.onSave).not.toHaveBeenCalled();
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

describe("a wallpaper the workspace no longer offers", () => {
  const ARCS = { kind: "bundled" as const, name: "arcs", url: "/wallpapers/bundled/arcs" };
  const RETIRED = { kind: "bundled" as const, name: "retired" };

  function pressed(): string[] {
    return [...card().querySelectorAll<HTMLButtonElement>("[data-wallpaper]")]
      .filter((swatch) => swatch.getAttribute("aria-pressed") === "true")
      .map((swatch) => swatch.getAttribute("data-wallpaper") as string);
  }

  it("reads as Default rather than leaving every swatch unlit", () => {
    render({
      desktop: desktopRecord("home", { name: "Home", wallpaper: RETIRED }),
      wallpapers: [ARCS],
    });
    expect(pressed()).toEqual(["default"]);
  });

  it("is saved as the default it already draws, so the dead reference is cleared", async () => {
    const attrs = render({
      desktop: desktopRecord("home", { name: "Home", wallpaper: RETIRED }),
      wallpapers: [ARCS],
    });
    (card().querySelector(".desktop-settings-save") as HTMLButtonElement).click();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith("Home", expect.any(String), expect.any(Number), null, {
      theme: null,
      isWorkspaceDefault: false,
    });
  });

  it("leaves a wallpaper that is still on offer selected and saved as itself", async () => {
    const attrs = render({
      desktop: desktopRecord("home", { name: "Home", wallpaper: { kind: "bundled", name: "arcs" } }),
      wallpapers: [ARCS],
    });
    expect(pressed()).toEqual(["bundled:arcs"]);
    (card().querySelector(".desktop-settings-save") as HTMLButtonElement).click();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith(
      "Home",
      expect.any(String),
      expect.any(Number),
      { kind: "bundled", name: "arcs" },
      { theme: null, isWorkspaceDefault: false },
    );
  });
});

describe("the Theme row's pictures", () => {
  it("draws the app's own icon for a theme that draws none of its own, and the theme's icon otherwise", () => {
    const files = appRecord("files", {
      icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"><rect width="24" height="24" fill="#123456"/></svg>',
    });
    render({
      previewApp: files,
      themes: themeCatalog(
        themeRecord("standard", { name: "Standard", icons: null }),
        themeRecord("mac-classic", { name: "Classic Mac" }),
      ),
    });

    const pictures = Array.from(card().querySelectorAll("[data-theme-picture]")).map((p) =>
      p.getAttribute("data-theme-picture"),
    );
    expect(pictures).toContain("standard");
    expect(card().querySelector('[data-theme-picture="standard"] rect')?.getAttribute("fill")).toBe("#123456");
  });
});

describe("Make your own...", () => {
  it("offers a tile that drafts the theme into a chat, when a chat can take the draft", () => {
    const onMakeTheme = vi.fn();
    render({ onMakeTheme });

    (card().querySelector("[data-make-theme]") as HTMLElement).click();

    expect(onMakeTheme).toHaveBeenCalledTimes(1);
    expect(card().querySelector("[data-make-theme-hint]")).toBeNull();
  });

  it("says where to ask instead, with no tile, when no chat can take the draft", () => {
    render({ onMakeTheme: null });

    expect(card().querySelector("[data-make-theme]")).toBeNull();
    expect(card().querySelector("[data-make-theme-hint]")?.textContent).toBe(MAKE_THEME_IN_CHAT_HINT);
  });
});
