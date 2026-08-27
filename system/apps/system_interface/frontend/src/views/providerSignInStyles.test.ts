/**
 * Every color utility in the ported sign-in styles has to resolve to a real token.
 *
 * Tailwind v4 emits NOTHING for an unknown color utility -- no build error, no lint error,
 * no type error. `hover:bg-fill-hover` against an undefined `--color-fill-hover` simply has
 * no hover state, and nothing in review says so. This file is ported from a mockup whose
 * token names differ from this app's, so that is the failure mode it invites.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import * as css from "./providerSignInStyles";

const STYLE_SHEET = readFileSync(join(__dirname, "..", "style.css"), "utf8");

/** Every `--color-*` name the stylesheet defines, in either @theme block. */
function definedColorTokens(): Set<string> {
  return new Set([...STYLE_SHEET.matchAll(/--color-([a-z0-9-]+)\s*:/g)].map((m) => m[1]));
}

/** Every color utility used across the exported class strings, minus arbitrary values. */
function usedColorTokens(): Set<string> {
  const used = new Set<string>();
  for (const value of Object.values(css)) {
    if (typeof value !== "string") continue;
    for (const cls of value.split(/\s+/)) {
      // Strip variants ("hover:", "focus:") and any leading "-".
      const bare = cls.slice(cls.lastIndexOf(":") + 1);
      const match = /^(?:text|bg|border|ring|from|to|decoration|outline|divide)-(.+)$/.exec(bare);
      if (match === null) continue;
      const token = match[1];
      // Arbitrary values carry their own color; sizes and styles are not colors at all.
      if (token.startsWith("[") || /^(\d|xs$|sm$|base$|lg$|xl$|\dxl$)/.test(token)) continue;
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
        ].includes(token)
      )
        continue;
      used.add(token);
    }
  }
  return used;
}

describe("the ported sign-in styles", () => {
  it("uses only color tokens this app defines", () => {
    const defined = definedColorTokens();
    const missing = [...usedColorTokens()].filter(
      // Tailwind ships its own palette (white, black, red-600, ...); only names that look
      // like OUR tokens need to be in our stylesheet.
      (token) => !defined.has(token) && !/^(white|black|transparent|current|[a-z]+-\d{2,3})$/.test(token),
    );
    expect(missing).toEqual([]);
  });

  it("defines the mockup's token names, so its class strings port unchanged", () => {
    const defined = definedColorTokens();
    for (const token of [
      "primary",
      "secondary",
      "tertiary",
      "subtle",
      "default",
      "strong",
      "fill-hover",
      "fill-subtle",
      "fill-active",
      "surface-primary",
      "surface-overlay",
    ]) {
      expect(defined, `--color-${token} is missing`).toContain(token);
    }
  });

  it("defines the mockup's @utility ramp and shadows", () => {
    // Colour was only half of it. The mockup also bundles size/weight/line-height into
    // `type-*` utilities and its elevation into `shadow-*`, and the combo card uses both --
    // `type-helper` eleven times in ModelBar.tsx alone. Same silent failure mode: Tailwind v4
    // emits nothing for an unknown utility, so a ported row just takes the inherited size and
    // reads as nearly-right rather than wrong.
    for (const utility of ["type-heading", "type-label", "type-body", "type-helper", "type-section", "type-badge"]) {
      // The trailing brace matters: without it `type-helper` matches `type-helper-renamed`.
      expect(STYLE_SHEET, `@utility ${utility} is missing`).toContain(`@utility ${utility} {`);
    }
    for (const shadow of ["--shadow-raised", "--shadow-overlay"]) {
      expect(STYLE_SHEET, `${shadow} is missing`).toContain(shadow);
    }
  });
});
