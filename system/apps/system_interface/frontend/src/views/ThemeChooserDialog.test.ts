// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, desktopRecord, themeCatalog, themeRecord } from "../testing/records";
import { MAKE_THEME_IN_CHAT_HINT, THEME_PREVIEW_ELSEWHERE_HINT, ThemeChooserDialog } from "./ThemeChooserDialog";
import type { ThemeChooserDialogAttrs } from "./ThemeChooserDialog";

afterEach(unmountViews);

function render(overrides: Partial<ThemeChooserDialogAttrs> = {}): ThemeChooserDialogAttrs {
  const attrs: ThemeChooserDialogAttrs = {
    desktop: desktopRecord("home", { name: "Home" }),
    onSave: vi.fn(async () => undefined),
    onCancel: vi.fn(),
    onPreviewTheme: vi.fn(),
    isDesktopShown: true,
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
  mountView(() => m(ThemeChooserDialog, attrs));
  return attrs;
}

function card(): HTMLElement {
  return document.querySelector("[data-theme-chooser]") as HTMLElement;
}

/** Let the save promise settle and the view take its outcome. */
async function settled(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  m.redraw.sync();
}

describe("ThemeChooserDialog", () => {
  it("saves the picked theme", async () => {
    const attrs = render();
    (card().querySelector('[data-theme="windows-2000"]') as HTMLButtonElement).click();
    (card().querySelector(".theme-chooser-save") as HTMLButtonElement).click();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith({ theme: "windows-2000", isWorkspaceDefault: false });
  });

  it("previews a picked theme at once and drops the preview when the dialog closes", () => {
    const attrs = render();
    (card().querySelector('[data-theme="mac-classic"]') as HTMLButtonElement).click();
    expect(attrs.onPreviewTheme).toHaveBeenLastCalledWith("mac-classic");
    expect(attrs.onSave).not.toHaveBeenCalled();
    unmountViews();
    expect(attrs.onClearPreview).toHaveBeenCalledOnce();
  });

  it("says a picked theme shows only once the user is on the desktop, for a desktop not on screen", () => {
    render();
    expect(card().querySelector("[data-theme-preview-hint]")).toBeNull();
    unmountViews();

    render({ isDesktopShown: false });
    expect(card().querySelector("[data-theme-preview-hint]")?.textContent).toBe(THEME_PREVIEW_ELSEWHERE_HINT);
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

  it("says the themes are loading, rather than judging the desktop's theme, until they have been read", async () => {
    const attrs = render({ desktop: desktopRecord("home", { name: "Home", theme: "mac-classic" }), themes: null });

    expect(card().querySelector("[data-themes-loading]")?.textContent).toBe("Loading themes…");
    expect(card().querySelector("[data-theme-unavailable]")).toBeNull();
    expect(card().querySelector("[data-theme]")).toBeNull();
    (card().querySelector(".theme-chooser-save") as HTMLButtonElement).click();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith({ theme: "mac-classic", isWorkspaceDefault: false });
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
    (card().querySelector(".theme-chooser-save") as HTMLButtonElement).click();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith({ theme: "mac-classic", isWorkspaceDefault: true });
  });

  it("says a saved theme is not available, and offers no workspace default for it", async () => {
    const attrs = render({ desktop: desktopRecord("home", { name: "Home", theme: "gone" }) });

    expect(card().querySelector("[data-theme-unavailable]")?.textContent).toBe(
      "This desktop's theme, gone, is not available, so it wears the standard look.",
    );
    expect(card().querySelector("[data-theme-as-workspace-default]")).toBeNull();
    (card().querySelector(".theme-chooser-save") as HTMLButtonElement).click();
    await settled();
    expect(attrs.onSave).toHaveBeenCalledWith({ theme: "gone", isWorkspaceDefault: false });

    (card().querySelector('[data-theme="mac-classic"]') as HTMLButtonElement).click();
    m.redraw.sync();
    expect(card().querySelector("[data-theme-unavailable]")).toBeNull();
    expect(card().querySelector("[data-theme-as-workspace-default]")).not.toBeNull();
  });

  it("shows the shell's refusal of a save and lets the user try again", async () => {
    const attrs = render({ onSave: vi.fn(async () => Promise.reject(new Error("themes are read-only here"))) });
    (card().querySelector(".theme-chooser-save") as HTMLButtonElement).click();
    await settled();
    expect(card().textContent).toContain("themes are read-only here");
    expect((card().querySelector(".theme-chooser-save") as HTMLButtonElement).disabled).toBe(false);
    expect(attrs.onSave).toHaveBeenCalledTimes(1);
  });

  it("Escape cancels", () => {
    const attrs = render();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    m.redraw.sync();
    expect(attrs.onCancel).toHaveBeenCalledTimes(1);
  });
});

describe("the tiles' pictures", () => {
  it("draws the app's own icon for a theme that draws none of its own, and the theme's icon otherwise", () => {
    const files = appRecord("files", {
      icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"><rect width="24" height="24" fill="#123456"/></svg>',
    });
    render({
      previewApp: files,
      themes: themeCatalog(themeRecord("mac-classic", { name: "Classic Mac" })),
    });

    expect(
      card().querySelector('[data-theme="standard"] [data-theme-picture="standard"] rect')?.getAttribute("fill"),
    ).toBe("#123456");
    expect(card().querySelector('[data-theme="mac-classic"] img')?.getAttribute("src")).toBe(
      "/api/themes/mac-classic/icons/files.png?v=rev-mac-classic",
    );
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
