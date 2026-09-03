/**
 * Every utility in the shared class-string modules has to resolve to a real token.
 *
 * Tailwind v4 emits NOTHING for an unknown utility -- no build error, no lint error, no type
 * error. `hover:bg-fill-hover` against an undefined `--color-fill-hover` simply has no hover
 * state, and `type-helper` against a missing `@utility` silently takes the inherited size.
 * Nothing in review says so, and the surface just looks nearly-right.
 *
 * These modules are where that bites hardest: they hold long class strings far from the markup
 * that uses them, so a name that resolves to nothing is invisible at both ends.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import * as cardCss from "./modelCardStyles";
import * as signInCss from "./providerSignInStyles";

const STYLE_MODULES: Record<string, Record<string, unknown>> = {
  providerSignInStyles: signInCss,
  modelCardStyles: cardCss,
};

const STYLE_SHEET = readFileSync(join(__dirname, "..", "style.css"), "utf8");

/** Every `--color-*` name the stylesheet defines, in either @theme block. */
function definedColorTokens(): Set<string> {
  return new Set([...STYLE_SHEET.matchAll(/--color-([a-z0-9-]+)\s*:/g)].map((m) => m[1]));
}

/** Every class name used across one module's exported class strings, variants stripped. */
function usedClasses(module: Record<string, unknown>): string[] {
  const used: string[] = [];
  for (const value of Object.values(module)) {
    if (typeof value !== "string") continue;
    for (const cls of value.split(/\s+/)) {
      // Strip variants ("hover:", "focus:") and any leading "-".
      used.push(cls.slice(cls.lastIndexOf(":") + 1));
    }
  }
  return used;
}

/** Every color utility used across one module, minus arbitrary values. */
function usedColorTokens(module: Record<string, unknown>): Set<string> {
  const used = new Set<string>();
  for (const bare of usedClasses(module)) {
    const match = /^(?:text|bg|border|ring|from|to|decoration|outline|divide)-(.+)$/.exec(bare);
    if (match === null) continue;
    // `bg-primary/55` is the same token at an opacity; the suffix is not part of its name.
    const token = match[1].split("/")[0];
    // Arbitrary values carry their own color; sizes and styles are not colors at all.
    if (token.startsWith("[") || token.startsWith("(") || /^(\d|xs$|sm$|base$|lg$|xl$|\dxl$)/.test(token)) continue;
    if (
      [
        "left",
        "right",
        "center",
        "justify",
        "solid",
        "dashed",
        "none",
        "transparent",
        "current",
        "inherit",
        "clip",
        "ellipsis",
        "nowrap",
        "wrap",
        "balance",
        "pretty",
        // Border SIDES, which share the `border-` prefix but name an edge, not a colour.
        "t",
        "b",
        "l",
        "r",
        "x",
        "y",
        // `outline-offset-*` likewise: a distance under the `outline-` prefix.
        "offset-2",
      ].includes(token)
    )
      continue;
    used.add(token);
  }
  return used;
}

describe("the shared style modules", () => {
  for (const [name, module] of Object.entries(STYLE_MODULES)) {
    it(`uses only color tokens this app defines (${name})`, () => {
      const defined = definedColorTokens();
      const missing = [...usedColorTokens(module)].filter(
        // Tailwind ships its own palette (white, black, red-600, ...); only names that look
        // like OUR tokens need to be in our stylesheet.
        (token) => !defined.has(token) && !/^(white|black|transparent|current|[a-z]+-\d{2,3})$/.test(token),
      );
      expect(missing).toEqual([]);
    });

    it(`uses only type roles this app defines (${name})`, () => {
      const roles = new Set(usedClasses(module).filter((cls) => cls.startsWith("type-")));
      // The trailing brace matters: without it `type-helper` matches `type-helper-renamed`.
      const missing = [...roles].filter((role) => !STYLE_SHEET.includes(`@utility ${role} {`));
      expect(missing).toEqual([]);
    });
  }

  it("defines the elevation tokens the modules reach for", () => {
    for (const shadow of ["--shadow-raised", "--shadow-overlay"]) {
      expect(STYLE_SHEET, `${shadow} is missing`).toContain(shadow);
    }
  });
});
