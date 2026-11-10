// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { chatShortcutOfChord, setKeybindingSurface, setResolvedKeybindingsForTest } from "./keybindings";

afterEach(() => {
  setResolvedKeybindingsForTest(null);
  setKeybindingSurface("browser");
});

describe("chatShortcutOfChord", () => {
  it("answers the manifest's defaults until the backend answers, then the resolved chords", () => {
    expect(chatShortcutOfChord("alt+meta+n")).toBe("new-chat");
    expect(chatShortcutOfChord("meta+.")).toBe("stop-agent");
    expect(chatShortcutOfChord("enter")).toBeNull();

    setResolvedKeybindingsForTest({ app: "chat", chords_by_action: { "new-chat": ["f6"], "stop-agent": [] } });
    expect(chatShortcutOfChord("alt+meta+n")).toBeNull();
    expect(chatShortcutOfChord("f6")).toBe("new-chat");
    expect(chatShortcutOfChord("meta+.")).toBeNull();
  });

  it("runs a chord a browser keeps for itself only in the desktop app", () => {
    // Every platform's browser keeps Command-N / Ctrl+N for a new window.
    setResolvedKeybindingsForTest({ app: "chat", chords_by_action: { "new-chat": ["meta+n"] } });
    expect(chatShortcutOfChord("meta+n")).toBeNull();
    setKeybindingSurface("desktop");
    expect(chatShortcutOfChord("meta+n")).toBe("new-chat");
  });
});
