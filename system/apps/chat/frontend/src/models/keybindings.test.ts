// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import {
  CHAT_SHORTCUT_DEFAULTS,
  type ChatShortcut,
  chatShortcutOfChord,
  listenForChatShortcutKeys,
  runChatShortcutOfChord,
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

describe("runChatShortcutOfChord", () => {
  it("runs the shortcut a chord names, and nothing for any other chord", () => {
    const ran: ChatShortcut[] = [];

    runChatShortcutOfChord("meta+.", (shortcut) => ran.push(shortcut));
    runChatShortcutOfChord("enter", (shortcut) => ran.push(shortcut));

    expect(ran).toEqual(["stop-agent"]);
  });
});

describe("listenForChatShortcutKeys", () => {
  // The listener stays on the document for the rest of the file, so it is installed once.
  const ran: ChatShortcut[] = [];
  beforeAll(() => listenForChatShortcutKeys((shortcut) => ran.push(shortcut)));
  beforeEach(() => {
    ran.length = 0;
    // A function key reads the same on every platform the test runner may report.
    setResolvedKeybindingsForTest({ app: "chat", chords_by_action: { "new-chat": ["f6"], "stop-agent": [] } });
  });

  function press(key: string, options: { repeat?: boolean } = {}): KeyboardEvent {
    const event = new KeyboardEvent("keydown", { key, cancelable: true, bubbles: true, ...options });
    document.body.dispatchEvent(event);
    return event;
  }

  it("runs a shortcut's key once per press and takes it from the page", () => {
    const pressed = press("F6");
    const held = press("F6", { repeat: true });

    expect(ran).toEqual(["new-chat"]);
    expect(pressed.defaultPrevented).toBe(true);
    expect(held.defaultPrevented).toBe(true);
  });

  it("leaves a key no shortcut has alone", () => {
    const pressed = press("F7");

    expect(ran).toEqual([]);
    expect(pressed.defaultPrevented).toBe(false);
  });

  it("runs nothing for a key something took first, such as a workspace-wide chord", () => {
    const takeEveryKey = (event: Event): void => event.preventDefault();
    window.addEventListener("keydown", takeEveryKey, { capture: true });
    try {
      press("F6");
    } finally {
      window.removeEventListener("keydown", takeEveryKey, { capture: true });
    }

    expect(ran).toEqual([]);
  });
});
