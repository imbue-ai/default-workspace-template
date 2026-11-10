// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  CHAT_SHORTCUT_DEFAULTS,
  chatShortcutOfChord,
  setKeybindingSurface,
  setResolvedKeybindingsForTest,
} from "./keybindings";

const APP_TOML = join(__dirname, "../../../app.toml");

/** Each `[[keybindings]]` table's default chords in the manifest, by action id. */
function manifestDefaultChords(): Record<string, string[]> {
  const tables = readFileSync(APP_TOML, "utf8").split("[[keybindings]]").slice(1);
  return Object.fromEntries(
    tables.map((table) => {
      const id = /^id\s*=\s*"([^"]+)"/m.exec(table)?.[1];
      const chords = /^default_chords\s*=\s*(\[[^\]]*\])/m.exec(table)?.[1];
      if (id === undefined || chords === undefined) throw new Error(`${APP_TOML} has a keybinding without both`);
      return [id, JSON.parse(chords) as string[]];
    }),
  );
}

afterEach(() => {
  setResolvedKeybindingsForTest(null);
  setKeybindingSurface("browser");
});

describe("CHAT_SHORTCUT_DEFAULTS", () => {
  it("are the manifest's defaults, which run until the backend answers", () => {
    const declared = manifestDefaultChords();
    const shortcuts = Object.keys(CHAT_SHORTCUT_DEFAULTS);
    expect(CHAT_SHORTCUT_DEFAULTS).toEqual(Object.fromEntries(shortcuts.map((id) => [id, declared[id]])));
  });
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
