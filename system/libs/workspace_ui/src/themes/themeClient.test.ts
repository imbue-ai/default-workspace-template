// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  THEME_ATTRIBUTE,
  THEME_LINK_ATTRIBUTE,
  THEME_REVISION_ATTRIBUTE,
  THEME_STORAGE_KEY,
  currentTheme,
  isCurrentThemeRemembered,
  isThemeRef,
  mirrorParentTheme,
  onThemeChanged,
  rememberedTheme,
  resetThemeClientForTests,
  themeBundleUrl,
  wearTheme,
  wearThemeFromMessage,
} from "./themeClient";
import { themeBoot, themeBootScript } from "./themeBoot";

afterEach(() => {
  resetThemeClientForTests();
  document.head.replaceChildren();
});

function themeLinks(): HTMLLinkElement[] {
  return Array.from(document.head.querySelectorAll<HTMLLinkElement>(`link[${THEME_LINK_ATTRIBUTE}]`));
}

function settleLink(link: HTMLLinkElement, outcome: "load" | "error"): void {
  link.dispatchEvent(new Event(outcome));
}

describe("wearTheme", () => {
  it("loads the theme's bundle as the last stylesheet, marks the page, and remembers the theme", async () => {
    const own = document.createElement("link");
    own.rel = "stylesheet";
    document.head.appendChild(own);

    const worn = wearTheme({ id: "mac-classic", revision: "abc" });
    const [link] = themeLinks();
    settleLink(link, "load");
    await worn;

    expect(link.getAttribute("href")).toBe("/_static/themes/mac-classic/theme.css?v=abc");
    expect(document.head.lastElementChild).toBe(link);
    expect(document.documentElement.getAttribute(THEME_ATTRIBUTE)).toBe("mac-classic");
    expect(document.documentElement.getAttribute(THEME_REVISION_ATTRIBUTE)).toBe("abc");
    expect(currentTheme()).toEqual({ id: "mac-classic", revision: "abc" });
    expect(JSON.parse(window.localStorage.getItem(THEME_STORAGE_KEY) ?? "null")).toEqual({
      id: "mac-classic",
      revision: "abc",
    });
  });

  it("keeps the old stylesheet until the new one has loaded, so a switch never shows the page unstyled", async () => {
    const first = wearTheme({ id: "mac-classic", revision: "1" });
    settleLink(themeLinks()[0], "load");
    await first;

    const second = wearTheme({ id: "windows-2000", revision: "2" });
    expect(themeLinks().map((link) => link.getAttribute(THEME_LINK_ATTRIBUTE))).toEqual([
      "mac-classic",
      "windows-2000",
    ]);
    settleLink(themeLinks()[1], "load");
    await second;

    expect(themeLinks().map((link) => link.getAttribute(THEME_LINK_ATTRIBUTE))).toEqual(["windows-2000"]);
  });

  it("drops a theme still loading when another is worn, settling it, whichever stylesheet loads first", async () => {
    const first = wearTheme({ id: "mac-classic", revision: "1" });
    settleLink(themeLinks()[0], "load");
    await first;

    const preview = wearTheme({ id: "windows-2000", revision: "2" });
    const previewLink = themeLinks()[1];
    const back = wearTheme({ id: "mac-classic", revision: "1" });
    await preview;
    expect(themeLinks().map((link) => link.getAttribute(THEME_LINK_ATTRIBUTE))).toEqual([
      "mac-classic",
      "mac-classic",
    ]);
    // A late error from the dropped link leaves the page's stylesheets alone.
    settleLink(previewLink, "error");
    expect(themeLinks()).toHaveLength(2);
    settleLink(themeLinks()[1], "load");
    await back;

    expect(themeLinks()).toHaveLength(1);
    expect(currentTheme()).toEqual({ id: "mac-classic", revision: "1" });
  });

  it("settles a repeated wear of a theme still loading only once its stylesheet has loaded", async () => {
    const worn = wearTheme({ id: "mac-classic", revision: "1" });
    let isAgainSettled = false;
    const again = wearTheme({ id: "mac-classic", revision: "1" }).then(() => {
      isAgainSettled = true;
    });
    await Promise.resolve();
    expect(isAgainSettled).toBe(false);

    settleLink(themeLinks()[0], "load");
    await worn;
    await again;
    expect(isAgainSettled).toBe(true);
  });

  it("holds the first frame for the bundle only when asked, marking the link before it is inserted", async () => {
    const append = document.head.appendChild.bind(document.head);
    let blockingWhenInserted: string | null = null;
    const spy = vi.spyOn(document.head, "appendChild").mockImplementation(<T extends Node>(node: T): T => {
      blockingWhenInserted = (node as unknown as Element).getAttribute("blocking");
      return append(node);
    });
    const worn = wearTheme({ id: "mac-classic", revision: "1" }, document, { isRenderBlocking: true });
    spy.mockRestore();
    settleLink(themeLinks()[0], "load");
    await worn;

    expect(blockingWhenInserted).toBe("render");
    const plain = wearTheme({ id: "windows-2000", revision: "1" });
    expect(themeLinks()[1].hasAttribute("blocking")).toBe(false);
    settleLink(themeLinks()[1], "load");
    await plain;
  });

  it("leaves the remembered theme alone for a page that only shows a theme", async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ id: "mac-classic", revision: "1" }));

    const worn = wearTheme({ id: "windows-2000", revision: "2" }, document, { isRemembered: false });
    settleLink(themeLinks()[0], "load");
    await worn;

    expect(currentTheme()).toEqual({ id: "windows-2000", revision: "2" });
    expect(rememberedTheme()).toEqual({ id: "mac-classic", revision: "1" });
  });

  it("drops a bundle that fails to load, which leaves the standard look", async () => {
    const worn = wearTheme({ id: "gone", revision: "1" });
    settleLink(themeLinks()[0], "error");
    await worn;

    expect(themeLinks()).toEqual([]);
  });

  it("wears the standard look by removing every theme stylesheet, and tells its listeners once per change", async () => {
    const heard: string[] = [];
    onThemeChanged((theme) => heard.push(theme.id));
    const themed = wearTheme({ id: "mac-classic", revision: "1" });
    settleLink(themeLinks()[0], "load");
    await themed;

    await wearTheme({ id: "standard", revision: "" });
    await wearTheme({ id: "standard", revision: "" });

    expect(themeLinks()).toEqual([]);
    expect(heard).toEqual(["mac-classic", "standard"]);
    expect(themeBundleUrl({ id: "standard", revision: "" })).toBeNull();
  });

  it("wears a previewed theme from the shell without remembering it, and remembers it once it is saved", async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ id: "mac-classic", revision: "1" }));
    const heard: [string, boolean][] = [];
    onThemeChanged((theme, isRemembered) => heard.push([theme.id, isRemembered]));

    const previewed = wearThemeFromMessage({ theme: "windows-2000", revision: "2", isPreview: true });
    settleLink(themeLinks()[0], "load");
    await previewed;
    expect(currentTheme()).toEqual({ id: "windows-2000", revision: "2" });
    expect(isCurrentThemeRemembered()).toBe(false);
    expect(rememberedTheme()).toEqual({ id: "mac-classic", revision: "1" });

    await wearThemeFromMessage({ theme: "windows-2000", revision: "2", isPreview: false });
    expect(themeLinks()).toHaveLength(1);
    expect(isCurrentThemeRemembered()).toBe(true);
    expect(rememberedTheme()).toEqual({ id: "windows-2000", revision: "2" });
    expect(heard).toEqual([
      ["windows-2000", false],
      ["windows-2000", true],
    ]);
  });

  it("ignores a shell message that does not name a theme", async () => {
    await wearThemeFromMessage({ theme: "../../etc", revision: "1" });
    await wearThemeFromMessage({ theme: 42 });

    expect(themeLinks()).toEqual([]);
    expect(currentTheme().id).toBe("standard");
    expect(isThemeRef({ id: "Mac Classic", revision: "" })).toBe(false);
  });
});

describe("mirrorParentTheme", () => {
  /** Frame this window under a same-origin parent whose root is `parentRoot`. */
  function framedUnder(parentRoot: HTMLElement): void {
    const parentWindow = { document: { documentElement: parentRoot } };
    Object.defineProperty(window, "parent", { value: parentWindow, configurable: true });
  }

  afterEach(() => {
    Object.defineProperty(window, "parent", { value: window, configurable: true });
  });

  it("answers false on a page no other page frames", () => {
    expect(mirrorParentTheme()).toBe(false);
    expect(currentTheme().id).toBe("standard");
  });

  it("wears the parent's theme without remembering it, and follows the parent's changes", async () => {
    const parentRoot = document.createElement("html");
    parentRoot.setAttribute(THEME_ATTRIBUTE, "mac-classic");
    parentRoot.setAttribute(THEME_REVISION_ATTRIBUTE, "1");
    framedUnder(parentRoot);

    expect(mirrorParentTheme()).toBe(true);
    expect(currentTheme()).toEqual({ id: "mac-classic", revision: "1" });
    expect(themeLinks()[0].getAttribute("href")).toBe("/_static/themes/mac-classic/theme.css?v=1");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();

    parentRoot.setAttribute(THEME_ATTRIBUTE, "windows-2000");
    parentRoot.setAttribute(THEME_REVISION_ATTRIBUTE, "2");
    // The parent's attributes are watched by a MutationObserver, which reports after the current task.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(currentTheme()).toEqual({ id: "windows-2000", revision: "2" });
  });

  it("stops following the parent once the page goes away, so the parent does not keep it alive", async () => {
    const parentRoot = document.createElement("html");
    parentRoot.setAttribute(THEME_ATTRIBUTE, "mac-classic");
    framedUnder(parentRoot);
    mirrorParentTheme();

    window.dispatchEvent(new Event("pagehide"));
    parentRoot.setAttribute(THEME_ATTRIBUTE, "windows-2000");
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(currentTheme().id).toBe("mac-classic");
  });
});

describe("the boot script", () => {
  function runBootScript(): void {
    new Function(themeBootScript())();
  }

  it("wears the remembered theme before the page's own code runs", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ id: "windows-2000", revision: "r9" }));

    runBootScript();

    const [link] = themeLinks();
    expect(link.getAttribute("href")).toBe("/_static/themes/windows-2000/theme.css?v=r9");
    expect(link.getAttribute("blocking")).toBe("render");
    expect(document.documentElement.getAttribute(THEME_ATTRIBUTE)).toBe("windows-2000");
    expect(rememberedTheme()).toEqual({ id: "windows-2000", revision: "r9" });
  });

  it("does nothing for the standard look, a malformed memory, or none", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ id: "standard", revision: "" }));
    runBootScript();
    window.localStorage.setItem(THEME_STORAGE_KEY, "{not json");
    runBootScript();
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ id: "<script>", revision: "" }));
    runBootScript();

    expect(themeLinks()).toEqual([]);
  });

  it("is what the Vite plugin injects at the end of every page's head", () => {
    const plugin = themeBoot();
    const transform = plugin.transformIndexHtml as () => { tag: string; children: string; injectTo: string }[];

    expect(transform()).toEqual([{ tag: "script", children: themeBootScript(), injectTo: "head" }]);
  });

  it("is adopted by the theme client: wearing the same theme waits for its stylesheet and adds no second one", async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ id: "mac-classic", revision: "1" }));
    runBootScript();
    const [booted] = themeLinks();
    let isShown = false;

    const worn = wearTheme({ id: "mac-classic", revision: "1" }).then(() => {
      isShown = true;
    });
    await Promise.resolve();
    const isShownBeforeLoad = isShown;
    settleLink(booted, "load");
    await worn;

    expect(isShownBeforeLoad).toBe(false);
    expect(themeLinks()).toEqual([booted]);
    expect(currentTheme()).toEqual({ id: "mac-classic", revision: "1" });
  });

  it("is dropped, while still loading, for another theme the page wears", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ id: "mac-classic", revision: "1" }));
    runBootScript();

    void wearTheme({ id: "windows-2000", revision: "2" });

    expect(themeLinks().map((link) => link.getAttribute(THEME_LINK_ATTRIBUTE))).toEqual(["windows-2000"]);
  });

  it("stays, once loaded, until another theme the page wears has loaded", async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ id: "mac-classic", revision: "1" }));
    runBootScript();
    const [booted] = themeLinks();
    Object.defineProperty(booted, "sheet", { value: new CSSStyleSheet() });

    const worn = wearTheme({ id: "windows-2000", revision: "2" });
    const both = themeLinks();
    settleLink(both[1], "load");
    await worn;

    expect(both.map((link) => link.getAttribute(THEME_LINK_ATTRIBUTE))).toEqual(["mac-classic", "windows-2000"]);
    expect(themeLinks().map((link) => link.getAttribute(THEME_LINK_ATTRIBUTE))).toEqual(["windows-2000"]);
  });
});
