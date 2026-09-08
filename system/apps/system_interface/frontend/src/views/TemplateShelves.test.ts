import { describe, expect, it } from "vitest";

import { railArrowTop, railFadeSides, railPageTarget, railPaging } from "./TemplateShelves";

describe("rail paging", () => {
  it("offers an arrow only where there is somewhere to go", () => {
    expect(railPaging({ scrollLeft: 0, clientWidth: 600, scrollWidth: 1500 })).toEqual({
      canPageLeft: false,
      canPageRight: true,
    });
    expect(railPaging({ scrollLeft: 400, clientWidth: 600, scrollWidth: 1500 })).toEqual({
      canPageLeft: true,
      canPageRight: true,
    });
    expect(railPaging({ scrollLeft: 900, clientWidth: 600, scrollWidth: 1500 })).toEqual({
      canPageLeft: true,
      canPageRight: false,
    });
    expect(railPaging({ scrollLeft: 0, clientWidth: 600, scrollWidth: 600 })).toEqual({
      canPageLeft: false,
      canPageRight: false,
    });
  });

  it("pages one visible width along, clamped to the ends", () => {
    expect(railPageTarget({ scrollLeft: 0, clientWidth: 600, scrollWidth: 1500 }, 1)).toBe(600);
    expect(railPageTarget({ scrollLeft: 600, clientWidth: 600, scrollWidth: 1500 }, 1)).toBe(900);
    expect(railPageTarget({ scrollLeft: 900, clientWidth: 600, scrollWidth: 1500 }, -1)).toBe(300);
    expect(railPageTarget({ scrollLeft: 300, clientWidth: 600, scrollWidth: 1500 }, -1)).toBe(0);
  });
});

describe("which ends of a rail fade", () => {
  it("fades only the ends with more rail past them", () => {
    expect(railFadeSides({ canPageLeft: false, canPageRight: true })).toBe("end");
    expect(railFadeSides({ canPageLeft: true, canPageRight: true })).toBe("both");
    expect(railFadeSides({ canPageLeft: true, canPageRight: false })).toBe("start");
  });

  it("fades neither end of a rail that fits", () => {
    expect(railFadeSides({ canPageLeft: false, canPageRight: false })).toBe("none");
  });

  it("leaves a card resting against an edge unfaded, so its hover lift stays whole", () => {
    // The two cases that matter: the rail at rest (first card against the start) and paged to its
    // end (last card against the end). Neither end is scrollable towards, so neither is masked.
    expect(railFadeSides({ canPageLeft: false, canPageRight: true })).not.toContain("start");
    expect(railFadeSides({ canPageLeft: true, canPageRight: false })).not.toContain("end");
  });
});

describe("where a paging arrow's circle sits", () => {
  it("puts it level with the drawings once they have been measured", () => {
    expect(railArrowTop(96)).toBe("96px");
  });

  it("falls back to the middle of the sliver before there is anything to measure", () => {
    // A rail that has not been laid out reports 0, which is not a real centre: no drawing's middle
    // lands on the row's very top edge.
    expect(railArrowTop(0)).toBe("50%");
  });
});
