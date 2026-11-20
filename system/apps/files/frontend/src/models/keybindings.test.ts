import { afterEach, describe, expect, it } from "vitest";
import { detectChordPlatform } from "@imbue/workspace-ui/src/keybindings";
import { matchesKeybinding, setKeybindingSurface, setResolvedKeybindingsForTest } from "./keybindings";

/** The keystroke that spells `meta+t` here: Command on a Mac, Control elsewhere. */
function metaT(): KeyboardEvent {
  const isMac = detectChordPlatform() === "mac";
  return { key: "t", code: "KeyT", metaKey: isMac, ctrlKey: !isMac, altKey: false, shiftKey: false } as KeyboardEvent;
}

afterEach(() => {
  setResolvedKeybindingsForTest(null);
  setKeybindingSurface("browser");
});

describe("matchesKeybinding", () => {
  it("runs a chord a browser keeps for itself only once the shell says the workspace runs in the desktop app", () => {
    setResolvedKeybindingsForTest({ app: "files", chords_by_action: { "new-folder": ["meta+t"] } });

    expect(matchesKeybinding(metaT(), "new-folder")).toBe(false);

    setKeybindingSurface("desktop");
    expect(matchesKeybinding(metaT(), "new-folder")).toBe(true);
  });
});
