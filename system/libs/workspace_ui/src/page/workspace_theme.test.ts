import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

function tokenValues(cssPath: URL): Map<string, string> {
  const css = readFileSync(cssPath, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const values = new Map<string, string>();
  for (const match of css.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    values.set(match[1], match[2].replace(/\s+/g, " ").trim());
  }
  return values;
}

describe("the page kit's stylesheet", () => {
  it("declares the standard theme's tokens with the values the shared library gives them", () => {
    const kit = tokenValues(new URL("./workspace_theme.css", import.meta.url));
    const library = tokenValues(new URL("../base.css", import.meta.url));

    expect(kit.size).toBeGreaterThan(40);
    for (const [token, value] of kit) {
      expect([token, library.get(token)]).toEqual([token, value]);
    }
  });

  it("declares every color token the shared library has", () => {
    const kit = tokenValues(new URL("./workspace_theme.css", import.meta.url));
    const library = tokenValues(new URL("../base.css", import.meta.url));

    const libraryColors = [...library.keys()].filter((token) => token.startsWith("--c-"));
    expect(libraryColors.filter((token) => !kit.has(token))).toEqual([]);
  });
});
