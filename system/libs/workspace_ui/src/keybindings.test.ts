// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { chordFromKeydown, keydownMatchesAction, keydownMatchesChords } from "./keybindings";

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
