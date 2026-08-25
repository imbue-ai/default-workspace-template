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
  // Zero raw hex outside @theme: every colour is a token. The former leftovers
  // resolved -- warm login/composer red unified onto a (warmed) --color-danger,
  // the amber waiting-dot onto --color-warning, the minds-tooltip black/white
  // onto text-primary/surface/on-accent, and the progress greys onto the neutral
  // text greys. Keep this at 0: new raw hex must become a token (or move here
  // with a design-system-exception comment).
  hexOutsideTheme: 0,
  // Tightened after the type-system phase: all text font-sizes now use
  // var(--font-size-*). Tightened again by the P3 icon-button migration, which
  // moved the share-modal close "×" onto the .btn--icon primitive (dropping its
  // bespoke 18px glyph size). The remaining 3 are icon glyphs (chevrons, +) that
  // are sized independently of the text scale, each marked design-system-exception.
  fontSizePx: 3,
  // Tightened after the radius de-dup: raw 6px border-radii now use
  // var(--radius-base). Tightened again by the P3 button-primitive migration,
  // which deleted four bespoke button families' raw-px radii (terminal-banner
  // 4px; queued-action, composer-under-bar and claude-login 8px). Tightened again
  // by the icon-button migration, which dropped the share-modal close-×'s bespoke
  // 4px radius (the round composer buttons use border-radius:50%, not px).
  // Tightened again by the badge primitive, which folded three bespoke badges'
  // radii (two 10px + one 4px) into the single .badge (one 10px). Tightened again
  // by the input primitive, which deleted the claude-login-input's bespoke 8px
  // radius (the shared .input uses var(--radius-base)). Tightened again by the P4
  // modal shell: the per-feature dialogs' bespoke 12px card radii collapse into the
  // single .modal-card. Tightened again as the last custom-url dialogs moved onto
  // the shell and the bespoke .custom-url-dialog block was deleted (its 12px card
  // radius). Tightened again by the radius scale (--radius-3/-4/-8/-10/-12/-pill,
  // named value-first to avoid Tailwind's rounded-{sm,md,lg} utilities used in the
  // views): every exact-match px radius moved onto a token (incl. the user bubble's
  // 4px tail corner). The last 2 are genuine one-offs kept raw with a
  // design-system-exception each -- a 1px insertion caret (half its 2px width) and
  // a 2px focus-ring radius on an inline link. (50% circles stay the raw idiom;
  // percentages are not counted by this ratchet.)
  borderRadiusPx: 2,
  // Tightened by the tooltip primitive: the three duplicated per-component
  // `[data-tooltip]::after` bubbles (each with `z-index: 1000`) were consolidated
  // into one generic `[data-tooltip]::after`, dropping two z-index literals.
  // Tightened again by the P4 modal shell: the new shared `.modal-overlay` reads
  // `var(--z-overlay)`, and each per-feature dialog overlay's old raw `z-index:
  // 10000` (plus the image lightbox's) now uses that token as it moves onto the
  // shell. Tightened again as the last custom-url dialogs moved onto the shell and
  // the bespoke .custom-url-dialog block (its raw z-index: 10000) was deleted.
  // Tightened again by the z-index scale (--z-content/-sticky/-dropdown/-overlay/
  // -tooltip): the five literals with an exact layer name (10001, 1000, 100, and
  // two 1s) moved onto the tokens. The remaining 6 have no layer name in the
  // scale -- two mid-layer overlays at 50 (chat-drop, Claude sign-in modal) and
  // four "content + 1" at 2 (the si-live-surface that must beat dockview's own
  // z-index:1, plus three progress-view blocks lifted over the timeline thread) --
  // each marked design-system-exception at its site.
  zIndexLiteral: 6,
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
