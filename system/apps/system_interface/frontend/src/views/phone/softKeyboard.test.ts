// @vitest-environment jsdom
/**
 * A soft keyboard reads as up only on a touch phone, with a field or an app's frame focused, when the visual
 * viewport keeps the layout's width and loses more than a fifth of its height; nothing else on a phone or a desktop
 * reads as one, and once up it stays up until the visual viewport grows back.
 */
import { describe, expect, it } from "vitest";
import { isSoftKeyboardUp, isTypingFocus } from "./softKeyboard";
import type { ViewportReading } from "./softKeyboard";

// An iPhone upright, at rest.
const AT_REST: ViewportReading = {
  isCoarsePointer: true,
  isTypingFocus: true,
  layoutWidthPx: 393,
  layoutHeightPx: 852,
  visualWidthPx: 393,
  visualHeightPx: 852,
};
// The iPhone keyboard with its form bar over the bottom of the screen.
const KEYBOARD_UP: ViewportReading = { ...AT_REST, visualHeightPx: 450 };

describe("isSoftKeyboardUp", () => {
  it("reads a keyboard over a focused field on a touch phone, upright or on its side", () => {
    expect(isSoftKeyboardUp(KEYBOARD_UP, false)).toBe(true);
    const sideways = { ...AT_REST, layoutWidthPx: 852, visualWidthPx: 852, layoutHeightPx: 393, visualHeightPx: 200 };
    expect(isSoftKeyboardUp(sideways, false)).toBe(true);
  });

  it("reads no keyboard at rest, or under a shrink a status bar or a hardware keyboard's shortcut bar makes", () => {
    expect(isSoftKeyboardUp(AT_REST, false)).toBe(false);
    expect(isSoftKeyboardUp({ ...AT_REST, visualHeightPx: 800 }, false)).toBe(false);
    const iPad = { ...AT_REST, layoutWidthPx: 1032, visualWidthPx: 1032, layoutHeightPx: 1374, visualHeightPx: 1294 };
    expect(isSoftKeyboardUp(iPad, false)).toBe(false);
  });

  it("reads no keyboard on a desktop, whatever its viewports do", () => {
    expect(isSoftKeyboardUp({ ...KEYBOARD_UP, isCoarsePointer: false }, false)).toBe(false);
    expect(isSoftKeyboardUp({ ...KEYBOARD_UP, isCoarsePointer: false }, true)).toBe(false);
  });

  it("reads a pinch zoom, which narrows the visual viewport as it shortens it, as no keyboard", () => {
    expect(isSoftKeyboardUp({ ...AT_REST, visualWidthPx: 196, visualHeightPx: 426 }, false)).toBe(false);
    expect(isSoftKeyboardUp({ ...AT_REST, visualWidthPx: 196, visualHeightPx: 426 }, true)).toBe(false);
  });

  it("needs a typing focus to come up, and keeps up without one until the viewport grows back", () => {
    const unfocused = { ...KEYBOARD_UP, isTypingFocus: false };
    expect(isSoftKeyboardUp(unfocused, false)).toBe(false);
    expect(isSoftKeyboardUp(unfocused, true)).toBe(true);
    expect(isSoftKeyboardUp({ ...AT_REST, isTypingFocus: false }, true)).toBe(false);
    expect(isSoftKeyboardUp(AT_REST, true)).toBe(false);
  });
});

describe("isTypingFocus", () => {
  it("counts an app's frame and a writable text field, not a button or nothing", () => {
    expect(isTypingFocus(document.createElement("iframe"))).toBe(true);
    expect(isTypingFocus(document.createElement("textarea"))).toBe(true);
    expect(isTypingFocus(document.createElement("button"))).toBe(false);
    expect(isTypingFocus(null)).toBe(false);
  });
});
