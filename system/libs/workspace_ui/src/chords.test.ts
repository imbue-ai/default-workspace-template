import { describe, expect, it } from "vitest";
import { isNewTabChord } from "./chords";
import type { ChordKeys } from "./chords";

export const chordKeys = (over: Partial<ChordKeys>): ChordKeys => ({
  key: "t",
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  ...over,
});

/** Every combination `app_contract.test.ts` replays against the contract's own private copy. */
export const NEW_TAB_CHORD_CASES: readonly Partial<ChordKeys>[] = [
  {},
  { metaKey: true },
  { ctrlKey: true },
  { key: "T", metaKey: true },
  { key: "T", ctrlKey: true },
  { key: "n", metaKey: true },
  { key: "n", ctrlKey: true },
  { metaKey: true, shiftKey: true },
  { metaKey: true, altKey: true },
  { ctrlKey: true, shiftKey: true },
  { metaKey: true, ctrlKey: true },
];

describe("isNewTabChord", () => {
  it("takes Cmd+T on an Apple platform and Ctrl+T elsewhere", () => {
    expect(isNewTabChord(chordKeys({ metaKey: true }), true)).toBe(true);
    expect(isNewTabChord(chordKeys({ ctrlKey: true }), false)).toBe(true);
  });

  it("ignores the other platform's chord, so Ctrl+T stays text editing on a Mac", () => {
    expect(isNewTabChord(chordKeys({ ctrlKey: true }), true)).toBe(false);
    expect(isNewTabChord(chordKeys({ metaKey: true }), false)).toBe(false);
  });

  it("takes the shifted key a caps-lock or Shift press reports", () => {
    expect(isNewTabChord(chordKeys({ key: "T", metaKey: true }), true)).toBe(true);
  });

  it("leaves every neighbouring chord alone", () => {
    expect(isNewTabChord(chordKeys({}), true)).toBe(false);
    expect(isNewTabChord(chordKeys({ key: "n", metaKey: true }), true)).toBe(false);
    expect(isNewTabChord(chordKeys({ metaKey: true, shiftKey: true }), true)).toBe(false);
    expect(isNewTabChord(chordKeys({ metaKey: true, altKey: true }), true)).toBe(false);
    expect(isNewTabChord(chordKeys({ metaKey: true, ctrlKey: true }), true)).toBe(false);
  });
});
