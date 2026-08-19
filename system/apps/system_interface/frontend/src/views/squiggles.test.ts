import { describe, expect, it } from "vitest";
import { compositeSquiggleMarkup, monogramMarkup, squiggleMarkup, SQUIGGLE_GLYPHS } from "./squiggles";

describe("compositeSquiggleMarkup", () => {
  it("draws the five sprites, each in its own transformed group", () => {
    const markup = compositeSquiggleMarkup(20);
    const groups = markup.match(/<g transform="/g);
    expect(groups).toHaveLength(5);
    expect(markup.match(/<path /g)).toHaveLength(5);
  });

  it("keeps each sprite's signature color, so the cluster is multicolor", () => {
    const markup = compositeSquiggleMarkup(20);
    // The sprite table picks glyphs 0, 3, 5, 7 and 9 -- five different colors.
    for (const index of [0, 3, 5, 7, 9]) {
      expect(markup).toContain(`stroke="${SQUIGGLE_GLYPHS[index].color}"`);
    }
    expect(markup).not.toContain(SQUIGGLE_GLYPHS[1].color);
  });

  it("centers the sprite that sits on the spread origin", () => {
    // Glyph 9 is placed at (52, 51), which is the origin the other four are
    // spread around, so it lands dead center on the 100-unit canvas whatever
    // the spread is.
    expect(compositeSquiggleMarkup(20)).toContain('transform="translate(50 50) rotate(0)');
  });

  it("pre-divides the stroke so every sprite renders at the same weight", () => {
    const markup = compositeSquiggleMarkup(20);
    for (const index of [0, 3, 5, 7, 9]) {
      const [, , w, h] = SQUIGGLE_GLYPHS[index].box;
      const scale = (46 * 1.35) / Math.max(w, h);
      expect(markup).toContain(`stroke-width="${6.5 / scale}"`);
    }
  });

  it("sizes the svg while keeping the fixed canvas", () => {
    const markup = compositeSquiggleMarkup(16);
    expect(markup).toContain('width="16" height="16"');
    expect(markup).toContain('viewBox="0 0 100 100"');
  });
});

describe("squiggleMarkup", () => {
  it("wraps the glyph index so any integer resolves to a real glyph", () => {
    const count = SQUIGGLE_GLYPHS.length;
    expect(squiggleMarkup(count, null, 16)).toBe(squiggleMarkup(0, null, 16));
    expect(squiggleMarkup(-1, null, 16)).toBe(squiggleMarkup(count - 1, null, 16));
    // An index a stale client could have persisted, far outside the table.
    expect(squiggleMarkup(3 * count + 2, null, 16)).toBe(squiggleMarkup(2, null, 16));
  });

  it("keeps the glyph's own color unless one is given", () => {
    expect(squiggleMarkup(0, null, 16)).toContain(`stroke="${SQUIGGLE_GLYPHS[0].color}"`);
    expect(squiggleMarkup(0, "#123456", 16)).toContain('stroke="#123456"');
  });
});

describe("monogramMarkup", () => {
  it("paints the tile in the project's color and shows the first letter", () => {
    const markup = monogramMarkup("newsreader", "#0b292b", 18);
    expect(markup).toContain('fill="#0b292b"');
    expect(markup).toContain(">N</text>");
    expect(markup).toContain('width="18" height="18"');
  });

  it("flips the letter white on a dark tile and black on a light one", () => {
    expect(monogramMarkup("Zen Box", "#0b292b", 18)).toContain('fill="#ffffff"');
    expect(monogramMarkup("Zen Box", "#fcefd4", 18)).toContain('fill="#000000"');
  });

  it("flips at the lightness the titlebar flips at", () => {
    // These two greys straddle CIE L* 49.44, the threshold the minds chrome's
    // titlebar uses; achromatic colors are where this math and the chrome's
    // `lch(from ...)` agree exactly.
    expect(monogramMarkup("Grey", "#757575", 18)).toContain('fill="#ffffff"');
    expect(monogramMarkup("Grey", "#767676", 18)).toContain('fill="#000000"');
  });

  it("reads shorthand hex the same as the long form", () => {
    expect(monogramMarkup("Grey", "#000", 18)).toContain('fill="#ffffff"');
    expect(monogramMarkup("Grey", "#fff", 18)).toContain('fill="#000000"');
  });

  it("falls back to the black letter for a color it cannot measure", () => {
    expect(monogramMarkup("Grey", "rebeccapurple", 18)).toContain('fill="#000000"');
  });

  it("escapes the letter and tolerates a nameless project", () => {
    expect(monogramMarkup("<b>", "#fcefd4", 18)).toContain(">&lt;</text>");
    expect(monogramMarkup("   ", "#fcefd4", 18)).toContain("></text>");
  });
});
