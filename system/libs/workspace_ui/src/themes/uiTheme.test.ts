// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { applyUiTheme, currentUiTheme, resetUiThemeForTests, retroAppIconUrl } from "./uiTheme";

afterEach(() => {
  resetUiThemeForTests();
  document.documentElement.removeAttribute("data-ui-theme");
});

describe("the UI theme the chrome hands the workspace", () => {
  it("wears a theme it knows on the root element, and says the look changed only when it did", () => {
    const root = document.documentElement;
    expect(applyUiTheme(root, "windows-2000")).toBe(true);
    expect(root.getAttribute("data-ui-theme")).toBe("windows-2000");
    expect(currentUiTheme()).toBe("windows-2000");
    expect(applyUiTheme(root, "windows-2000")).toBe(false);
  });

  it("reads a theme name from a newer chrome as the default look", () => {
    const root = document.documentElement;
    applyUiTheme(root, "mac-classic");
    expect(applyUiTheme(root, "beos")).toBe(true);
    expect(root.getAttribute("data-ui-theme")).toBe("default");
  });

  it("offers a pixel-art icon per app under a retro theme, with the generic one for an app it has none for", () => {
    const root = document.documentElement;
    expect(retroAppIconUrl("chat")).toBeNull();
    applyUiTheme(root, "mac-classic");
    const chat = retroAppIconUrl("chat");
    const generic = retroAppIconUrl("an-app-made-yesterday");
    expect(chat).not.toBeNull();
    expect(generic).not.toBeNull();
    expect(chat).not.toBe(generic);
    for (const name of ["chat", "terminal", "files", "browser", "getting_started"]) {
      expect(retroAppIconUrl(name), name).not.toBe(generic);
    }
  });
});
