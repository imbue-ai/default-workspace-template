// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { chatShortcutOfChord, setResolvedKeybindingsForTest } from "./keybindings";

afterEach(() => {
  setResolvedKeybindingsForTest(null);
});

describe("chatShortcutOfChord", () => {
  it("answers the manifest's defaults until the backend answers, then the resolved chords", () => {
    expect(chatShortcutOfChord("alt+meta+n")).toBe("new-chat");
    expect(chatShortcutOfChord("meta+.")).toBe("interrupt-agent");
    expect(chatShortcutOfChord("enter")).toBeNull();

    setResolvedKeybindingsForTest({ app: "chat", chords_by_action: { "new-chat": ["f6"], "interrupt-agent": [] } });
    expect(chatShortcutOfChord("alt+meta+n")).toBeNull();
    expect(chatShortcutOfChord("f6")).toBe("new-chat");
    expect(chatShortcutOfChord("meta+.")).toBeNull();
  });
});
