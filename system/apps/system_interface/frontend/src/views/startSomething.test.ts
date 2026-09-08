import { describe, expect, it } from "vitest";

import {
  START_OPTIONS,
  START_PAGE_SIZE,
  glyphTones,
  hasMoreStartOptions,
  nextStartCount,
  searchStartOptions,
  startGlyph,
  visibleStartOptions,
} from "./startSomething";

describe("the Start something table", () => {
  it("has unique keys, a prompt on every tile but the one that scrolls to the templates", () => {
    const keys = START_OPTIONS.map((option) => option.key);
    expect(new Set(keys).size).toBe(keys.length);
    const withoutPrompt = START_OPTIONS.filter((option) => option.prompt === null);
    expect(withoutPrompt.map((option) => option.key)).toEqual(["template"]);
    for (const option of START_OPTIONS) {
      expect(option.title).not.toBe("");
      expect(option.description).not.toBe("");
      expect(option.hue).toMatch(/^#[0-9a-f]{6}$/);
    }
  });

  it("keeps the learn-about-Minds and edit-Minds tiles behind the first page", () => {
    expect(START_OPTIONS.length).toBe(START_PAGE_SIZE + 2);
    expect(START_OPTIONS.slice(START_PAGE_SIZE).map((option) => option.key)).toEqual(["learn", "edit-minds"]);
  });
});

describe("paging", () => {
  it("shows a page at a time and never past the end", () => {
    expect(visibleStartOptions(START_OPTIONS, START_PAGE_SIZE).length).toBe(START_PAGE_SIZE);
    expect(visibleStartOptions(START_OPTIONS, 100).length).toBe(START_OPTIONS.length);
    expect(visibleStartOptions(START_OPTIONS, -3)).toEqual([]);
    expect(nextStartCount(6, 7)).toBe(7);
    expect(nextStartCount(6, 20)).toBe(12);
    expect(hasMoreStartOptions(6, 7)).toBe(true);
    expect(hasMoreStartOptions(7, 7)).toBe(false);
  });
});

describe("searchStartOptions", () => {
  it("finds tiles by title or description", () => {
    expect(searchStartOptions(START_OPTIONS, "routine").map((option) => option.key)).toEqual(["routine"]);
    expect(searchStartOptions(START_OPTIONS, "inbox").map((option) => option.key)).toEqual(["make-sense"]);
    expect(searchStartOptions(START_OPTIONS, "nothing at all")).toEqual([]);
  });
});

describe("startGlyph", () => {
  it("draws the tile's paths in the shared stroke frame at the asked size", () => {
    const markup = startGlyph(START_OPTIONS[0], 24, true);
    expect(markup.startsWith("<svg ")).toBe(true);
    expect(markup).toContain('width="24"');
    expect(markup).toContain(START_OPTIONS[0].glyphPaths);
  });

  it("tints the glyph in two tones of the tile's hue, and inherits the text colour when standing down", () => {
    const tones = glyphTones(START_OPTIONS[0]);
    expect(tones.stroke).toBe(`color-mix(in srgb, ${START_OPTIONS[0].hue}, black 30%)`);
    expect(tones.fill).toBe(`color-mix(in srgb, ${START_OPTIONS[0].hue}, white 75%)`);
    expect(startGlyph(START_OPTIONS[0], 24, true)).toContain(`stroke="${tones.stroke}"`);
    expect(startGlyph(START_OPTIONS[0], 24, false)).toContain('fill="none" stroke="currentColor"');
  });
});
