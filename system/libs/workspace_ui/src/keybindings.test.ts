// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import {
  chordDisplayLabel,
  chordFromKeydown,
  chordKeycapLabels,
  chordSpokenLabel,
  keydownMatchesAction,
  keydownMatchesChords,
} from "./keybindings";

function keydown(key: string, modifiers: Partial<KeyboardEventInit> = {}): KeyboardEvent {
  return new KeyboardEvent("keydown", { key, ...modifiers });
}

describe("chordFromKeydown", () => {
  it("spells modifiers in the fixed order with the lowercased key", () => {
    expect(chordFromKeydown(keydown("P", { ctrlKey: true, shiftKey: true }))).toBe("ctrl+shift+p");
    expect(chordFromKeydown(keydown("Enter", { metaKey: true }))).toBe("meta+enter");
    expect(chordFromKeydown(keydown("a"))).toBe("a");
    expect(chordFromKeydown(keydown("F2"))).toBe("f2");
    expect(chordFromKeydown(keydown("ArrowLeft", { altKey: true }))).toBe("alt+arrowleft");
  });

  it("spells the separator-unfriendly keys by name", () => {
    expect(chordFromKeydown(keydown(" ", { ctrlKey: true }))).toBe("ctrl+space");
    expect(chordFromKeydown(keydown("+", { metaKey: true }))).toBe("meta+plus");
  });

  it("answers null for bare modifiers and unnamed keys", () => {
    expect(chordFromKeydown(keydown("Control", { ctrlKey: true }))).toBeNull();
    expect(chordFromKeydown(keydown("Shift", { shiftKey: true }))).toBeNull();
    expect(chordFromKeydown(keydown("MediaPlayPause"))).toBeNull();
  });
});

describe("chordKeycapLabels", () => {
  it("draws the modifiers as Apple's symbols in the grammar's order on an Apple platform", () => {
    expect(chordKeycapLabels("ctrl+alt+shift+meta+n", true)).toEqual(["⌃", "⌥", "⇧", "⌘", "N"]);
    expect(chordKeycapLabels("meta+enter", true)).toEqual(["⌘", "↩"]);
    expect(chordKeycapLabels("meta+backspace", true)).toEqual(["⌘", "⌫"]);
  });

  it("spells the modifiers out elsewhere", () => {
    expect(chordKeycapLabels("ctrl+shift+n", false)).toEqual(["Ctrl", "Shift", "N"]);
    expect(chordKeycapLabels("meta+enter", false)).toEqual(["Meta", "Enter"]);
    expect(chordKeycapLabels("ctrl+backspace", false)).toEqual(["Ctrl", "Backspace"]);
  });

  it("draws function, arrow, punctuation, and named keys on every platform", () => {
    expect(chordKeycapLabels("f2", false)).toEqual(["F2"]);
    expect(chordKeycapLabels("alt+arrowup", false)).toEqual(["Alt", "↑"]);
    expect(chordKeycapLabels("ctrl+[", true)).toEqual(["⌃", "["]);
    expect(chordKeycapLabels("meta+plus", true)).toEqual(["⌘", "+"]);
    expect(chordKeycapLabels("escape", true)).toEqual(["Esc"]);
  });
});

describe("chordDisplayLabel", () => {
  it("runs Apple's symbols together, the way a Mac menu writes a shortcut", () => {
    expect(chordDisplayLabel("shift+meta+n", true)).toBe("⇧⌘N");
    expect(chordDisplayLabel("meta+enter", true)).toBe("⌘↩");
    expect(chordDisplayLabel("f2", true)).toBe("F2");
  });

  it("joins the names with plus signs elsewhere", () => {
    expect(chordDisplayLabel("ctrl+shift+n", false)).toBe("Ctrl+Shift+N");
    expect(chordDisplayLabel("alt+arrowup", false)).toBe("Alt+↑");
  });
});

describe("chordSpokenLabel", () => {
  it("names each key in words, joined with plus signs", () => {
    expect(chordSpokenLabel("shift+meta+n", true)).toBe("Shift+Command+N");
    expect(chordSpokenLabel("alt+arrowleft", true)).toBe("Option+Left Arrow");
    expect(chordSpokenLabel("ctrl+enter", false)).toBe("Ctrl+Enter");
    expect(chordSpokenLabel("delete", false)).toBe("Delete");
  });
});

describe("matching", () => {
  it("matches a keystroke against a chord list exactly", () => {
    expect(keydownMatchesChords(keydown("Enter", { ctrlKey: true }), ["ctrl+enter", "meta+enter"])).toBe(true);
    expect(keydownMatchesChords(keydown("Enter", { shiftKey: true }), ["ctrl+enter", "meta+enter"])).toBe(false);
    expect(keydownMatchesChords(keydown("Enter"), [])).toBe(false);
  });

  it("matches the named action only once the resolved chords are loaded", () => {
    const resolved = { app: "chat", chords_by_action: { "send-message": ["enter"] } };
    expect(keydownMatchesAction(keydown("Enter"), resolved, "send-message")).toBe(true);
    expect(keydownMatchesAction(keydown("Enter"), resolved, "unknown-action")).toBe(false);
    expect(keydownMatchesAction(keydown("Enter"), null, "send-message")).toBe(false);
  });
});
