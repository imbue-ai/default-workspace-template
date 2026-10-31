// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import { themeValue } from "./themeValues";

// The test document itself: jsdom parses the stylesheets only of a document with a window.
function addStyle(css: string, isTheme: boolean): void {
  const element = document.createElement("style");
  if (isTheme) element.setAttribute("data-workspace-theme", "");
  element.textContent = css;
  document.head.appendChild(element);
}

afterEach(() => {
  for (const element of Array.from(document.head.querySelectorAll("style"))) element.remove();
});

describe("themeValue", () => {
  it("answers null for every token when no theme is worn, whatever the page itself declares", () => {
    addStyle(":root { --c-accent: #2f6b4f; --c-fill-hover: rgb(0 0 0/.07); }", false);
    expect(themeValue("c-accent")).toBeNull();
    expect(themeValue("c-fill-hover")).toBeNull();
  });

  it("answers the theme's :root value for a token it sets, and null for one it leaves alone", () => {
    addStyle(":root { --c-accent: #2f6b4f; --c-bg: #fafaf8; }", false);
    addStyle(':root { --c-accent: #000080; } [data-part="button"] { --c-bg: #ffffff; }', true);
    expect(themeValue("c-accent")).toBe("#000080");
    // A token a theme sets only inside a part is that part's, not the page's.
    expect(themeValue("c-bg")).toBeNull();
  });

  it("follows a change of theme on the same page, the later stylesheet winning", () => {
    addStyle(":root { --c-accent: #000080; }", true);
    expect(themeValue("c-accent")).toBe("#000080");
    addStyle(":root { --c-accent: #000000; }", true);
    expect(themeValue("c-accent")).toBe("#000000");
  });
});
