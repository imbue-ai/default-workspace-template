import { describe, expect, it } from "vitest";
import { TOUR_EDGE, TOUR_GAP, placeBeside, placeByAvatar, placeUnderChrome } from "./tourPlacement";

const VIEWPORT = { width: 1200, height: 800 };
const CARD = { width: 360, height: 160 };

describe("placeByAvatar", () => {
  it("puts the card's bottom-left corner at the avatar's top-right, inside the viewport", () => {
    const avatar = { left: 24, top: 700, width: 72, height: 72 };
    expect(placeByAvatar(avatar, CARD, VIEWPORT)).toEqual({ left: 24 + 72 + TOUR_GAP, top: 700 - 160, arrow: null });
    const atTheEdge = { left: 1100, top: 40, width: 72, height: 72 };
    const placed = placeByAvatar(atTheEdge, CARD, VIEWPORT);
    expect(placed.left).toBe(VIEWPORT.width - CARD.width - TOUR_EDGE);
    expect(placed.top).toBe(TOUR_EDGE);
  });

  it("falls to the bottom-left corner when there is no avatar on screen", () => {
    expect(placeByAvatar(null, CARD, VIEWPORT)).toEqual({ left: TOUR_EDGE, top: 800 - 160 - TOUR_EDGE, arrow: null });
  });
});

describe("placeBeside", () => {
  it("sits below a target with room under it, centred on it, with the arrow on its top edge", () => {
    const target = { left: 500, top: 100, width: 100, height: 40 };
    const placed = placeBeside(target, CARD, VIEWPORT);
    expect(placed.top).toBe(100 + 40 + TOUR_GAP);
    expect(placed.left).toBe(550 - 180);
    expect(placed.arrow).toEqual({ side: "top", offset: 180 });
  });

  it("sits above a target near the bottom, and beside one too tall for either", () => {
    const low = { left: 500, top: 740, width: 100, height: 40 };
    const above = placeBeside(low, CARD, VIEWPORT);
    expect(above.top).toBe(740 - TOUR_GAP - 160);
    expect(above.arrow?.side).toBe("bottom");

    const tall = { left: 100, top: 20, width: 300, height: 760 };
    const right = placeBeside(tall, CARD, VIEWPORT);
    expect(right.left).toBe(100 + 300 + TOUR_GAP);
    expect(right.arrow?.side).toBe("left");

    const tallAtRight = { left: 800, top: 20, width: 390, height: 760 };
    const left = placeBeside(tallAtRight, CARD, VIEWPORT);
    expect(left.left).toBe(800 - TOUR_GAP - 360);
    expect(left.arrow?.side).toBe("right");
  });
});

describe("placeUnderChrome", () => {
  it("sits at the top edge on the control's side with the arrow pointing up", () => {
    expect(placeUnderChrome("left", CARD, VIEWPORT)).toEqual({
      left: 160,
      top: TOUR_EDGE,
      arrow: { side: "top", offset: 40 },
    });
    expect(placeUnderChrome("right", CARD, VIEWPORT)).toEqual({
      left: 1200 - 360 - TOUR_EDGE,
      top: TOUR_EDGE,
      arrow: { side: "top", offset: 320 },
    });
  });
});
