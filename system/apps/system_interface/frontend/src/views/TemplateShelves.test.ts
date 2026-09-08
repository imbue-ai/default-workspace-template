import { describe, expect, it } from "vitest";

import { railPageTarget, railPaging } from "./TemplateShelves";

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
