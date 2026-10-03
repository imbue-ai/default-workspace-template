// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { applyUiTheme, resetUiThemeForTests, retroAppIconUrl } from "@imbue/workspace-ui/src/themes/uiTheme";
import { appGlyph } from "./glyphs";

afterEach(() => resetUiThemeForTests());

const CHAT = { name: "chat", icon: "" };

describe("appGlyph", () => {
  it("draws an app's pixel-art icon, sized like any other, under a retro theme", () => {
    applyUiTheme(document.documentElement, "windows-2000");
    const markup = appGlyph(CHAT, 20);
    expect(markup).toContain(`href="${retroAppIconUrl("chat")}"`);
    expect(markup).toContain('width="20" height="20"');
    expect(markup.startsWith("<svg")).toBe(true);
  });

  it("draws the generic pixel-art icon for an app not in the registry", () => {
    applyUiTheme(document.documentElement, "mac-classic");
    expect(appGlyph(undefined, 20)).toContain(`href="${retroAppIconUrl("app")}"`);
  });

  it("keeps the app's own glyph in the default look", () => {
    applyUiTheme(document.documentElement, "default");
    expect(appGlyph(CHAT, 20)).not.toContain("<image");
  });
});
