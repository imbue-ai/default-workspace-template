import { describe, expect, it } from "vitest";

import { HOVER_GLYPH_GROUP, HOVER_LIFT_GROUP, HOVER_LIFT_TRANSITION, HOVER_SHADOW_SELF } from "./hoverLift";

describe("the New Tab hover recipes", () => {
  it("transitions the property a scale utility actually sets", () => {
    // A ``scale-*`` utility sets the standalone ``scale`` property rather than writing into
    // ``transform``, so a transition naming ``transform`` matches nothing: the growth lands in one
    // frame while the shadow eases in around it. This is exactly the bug that shipped once.
    expect(HOVER_LIFT_TRANSITION).toContain("scale");
    expect(HOVER_LIFT_TRANSITION).not.toContain("transform");
    for (const recipe of [HOVER_LIFT_GROUP, HOVER_GLYPH_GROUP]) {
      expect(recipe).toContain("scale-[");
      expect(recipe).toContain(HOVER_LIFT_TRANSITION);
    }
  });

  it("times every recipe alike", () => {
    for (const recipe of [HOVER_LIFT_GROUP, HOVER_SHADOW_SELF, HOVER_GLYPH_GROUP]) {
      expect(recipe).toContain(HOVER_LIFT_TRANSITION);
    }
  });

  it("keeps the tile itself still, and lets only its glyph grow", () => {
    expect(HOVER_SHADOW_SELF).not.toContain("scale-[");
    expect(HOVER_GLYPH_GROUP).toContain("group-hover:scale-[1.15]");
    // Grown from the left edge, so the glyph stays lined up with the text under it.
    expect(HOVER_GLYPH_GROUP).toContain("origin-left");
  });
});
