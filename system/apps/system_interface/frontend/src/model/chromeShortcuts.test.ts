import { describe, expect, it } from "vitest";
import { readChromeShortcuts } from "./chromeShortcuts";

const QUIT = { id: "quit", label: "Quit", defaultChords: ["meta+q"], chords: ["meta+q"] };

describe("readChromeShortcuts", () => {
  it("reads the actions, with no error when the message carries none", () => {
    expect(readChromeShortcuts({ actions: [QUIT] })).toEqual({ actions: [QUIT], error: null });
  });

  it("reads why the last save failed", () => {
    expect(readChromeShortcuts({ actions: [QUIT], error: "The account is offline" })?.error).toBe(
      "The account is offline",
    );
  });

  it("refuses a message not shaped as the catalog", () => {
    expect(readChromeShortcuts({})).toBeNull();
    expect(readChromeShortcuts({ actions: [{ ...QUIT, chords: "meta+q" }] })).toBeNull();
    expect(readChromeShortcuts({ actions: [{ ...QUIT, defaultChords: [""] }] })).toBeNull();
    expect(readChromeShortcuts({ actions: [{ ...QUIT, label: 3 }] })).toBeNull();
    expect(readChromeShortcuts({ actions: [QUIT], error: 3 })).toBeNull();
  });
});
