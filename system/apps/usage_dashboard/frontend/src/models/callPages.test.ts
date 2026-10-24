import { describe, expect, it } from "vitest";

import { pageCount, pageHolding, pageNumbers, pageOffset, pageRange, typedPage } from "./callPages";

describe("pages", () => {
  it("fills pages of the chosen size, with one page even with no calls", () => {
    expect([pageCount(0, 20), pageCount(20, 20), pageCount(21, 20), pageCount(45, 20)]).toEqual([1, 1, 2, 3]);
    expect(pageCount(45, 10)).toBe(5);
  });

  it("skips the pages before it", () => {
    expect([pageOffset(1, 20), pageOffset(3, 20), pageOffset(3, 50)]).toEqual([0, 40, 100]);
  });

  it("names the calls a page shows, ending short on the last page", () => {
    expect(pageRange(1, 20, 45)).toEqual([1, 20]);
    expect(pageRange(3, 20, 45)).toEqual([41, 45]);
    expect(pageRange(1, 20, 0)).toEqual([0, 0]);
  });

  it("keeps the first row being read in view when the page size changes", () => {
    // Page 3 of 20 starts at call 41, which is on page 1 of 50 and page 5 of 10.
    expect([pageHolding(40, 50), pageHolding(40, 10)]).toEqual([1, 5]);
  });
});

describe("typedPage", () => {
  it("takes a whole number, kept to the pages there are", () => {
    expect([typedPage("7", 20), typedPage(" 7 ", 20), typedPage("99", 20), typedPage("0", 20)]).toEqual([7, 7, 20, 1]);
  });

  it("refuses anything else", () => {
    expect([typedPage("", 20), typedPage("2.5", 20), typedPage("two", 20), typedPage("-3", 20)]).toEqual([
      null,
      null,
      null,
      null,
    ]);
  });
});

describe("pageNumbers", () => {
  it("lists every page when there are few", () => {
    expect(pageNumbers(1, 1)).toEqual([1]);
    expect(pageNumbers(2, 5)).toEqual([1, 2, 3, 4, 5]);
  });

  it("keeps the first and last pages and the current one's neighbours, with gaps between", () => {
    expect(pageNumbers(10, 20)).toEqual([1, "gap", 9, 10, 11, "gap", 20]);
  });

  it("keeps the row's length near either end", () => {
    expect(pageNumbers(1, 20)).toEqual([1, 2, 3, 4, "gap", 20]);
    expect(pageNumbers(20, 20)).toEqual([1, "gap", 17, 18, 19, 20]);
  });

  it("shows a single left-out page instead of a gap", () => {
    expect(pageNumbers(4, 20)).toEqual([1, 2, 3, 4, 5, "gap", 20]);
  });
});
