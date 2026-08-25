import { readFileSync } from "fs";
import { describe, expect, it } from "vitest";

/*
 * Design-system ratchet. Guards against the token/component sprawl documented in
 * ../../docs/design-system.md by counting raw values in style.css that should be
 * tokens. Each baseline may only DECREASE -- adding a raw value fails the suite.
 *
 * Escape hatch (see ../style_guide.md): if a deviation is genuinely necessary,
 * mark the site with a `design-system-exception: <reason>` comment AND raise the
 * matching baseline here with a comment explaining why, in the same change. Do
 * NOT dodge the regex by reformatting -- that hides the problem and is worse than
 * the raw value. When a phase tokenizes values, TIGHTEN the baseline to lock the
 * win in (that is the whole point of a ratchet).
 *
 * Counts are of occurrences (not distinct values): the goal is that new code
 * reaches for a token, so every additional literal must move a number here.
 */

const STYLE_CSS = new URL("./style.css", import.meta.url).pathname;

// Baselines recorded at the pre-sweep state (design-system phase P0). Lower is
// better; tighten after each migration phase.
const BASELINES = {
  hexOutsideTheme: 52,
  // Tightened after the type-system phase: all text font-sizes now use
  // var(--font-size-*). The remaining 4 are icon glyphs (chevrons, +, ×) that
  // are sized independently of the text scale, each marked design-system-exception.
  fontSizePx: 4,
  borderRadiusPx: 55,
  zIndexLiteral: 18,
} as const;

interface Counts {
  hexOutsideTheme: number;
  fontSizePx: number;
  borderRadiusPx: number;
  zIndexLiteral: number;
}

function countViolations(css: string): Counts {
  // Comments (including the design-system banner) are not code; strip them so a
  // hex/px mentioned in prose never counts.
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  // The @theme block is where colour/radius tokens are DEFINED as literals --
  // that is correct, so exclude it from the "raw value" counts.
  const theme = noComments.match(/@theme\s*\{[\s\S]*?\n\}/);
  const outside = theme ? noComments.replace(theme[0], "") : noComments;
  const count = (re: RegExp): number => (outside.match(re) || []).length;
  return {
    hexOutsideTheme: count(/#[0-9a-fA-F]{3,8}\b/g),
    fontSizePx: count(/font-size:\s*[\d.]+px/g),
    borderRadiusPx: count(/border-radius:[^;{}]*?\b[\d.]+px/g),
    zIndexLiteral: count(/z-index:\s*\d+/g),
  };
}

describe("design-system ratchet", () => {
  const counts = countViolations(readFileSync(STYLE_CSS, "utf-8"));

  it("@theme block is present (guards the exclusion logic)", () => {
    // If this fails, the exclusion above silently stopped working and every
    // token definition would be counted as a violation.
    const css = readFileSync(STYLE_CSS, "utf-8");
    expect(css).toMatch(/@theme\s*\{/);
  });

  for (const key of Object.keys(BASELINES) as (keyof typeof BASELINES)[]) {
    it(`${key}: no new raw values (<= baseline ${BASELINES[key]})`, () => {
      expect(
        counts[key],
        `Raw '${key}' in style.css rose to ${counts[key]} (baseline ${BASELINES[key]}). ` +
          `Use a token instead (see frontend/style_guide.md). If the deviation is truly ` +
          `necessary, mark the site with a "design-system-exception" comment and raise ` +
          `this baseline with a reason. If you REMOVED raw values, lower the baseline to ${counts[key]}.`,
      ).toBeLessThanOrEqual(BASELINES[key]);
    });
  }
});
