// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import {
  chordDisplayLabel,
  chordFromKeydown,
  chordKeycapLabels,
  chordNotices,
  chordSpokenLabel,
  detectChordPlatform,
  isChordHiddenOn,
  isRefusedChord,
  isWorkspaceWideChord,
  keydownMatchesAction,
  keydownMatchesChords,
  readsChordAsApple,
} from "./keybindings";

function keydown(key: string, modifiers: Partial<KeyboardEventInit> = {}): KeyboardEvent {
  return new KeyboardEvent("keydown", { key, ...modifiers });
}

describe("chordFromKeydown on a Mac", () => {
  it("spells modifiers in the fixed order with the lowercased key", () => {
    expect(chordFromKeydown(keydown("P", { ctrlKey: true, shiftKey: true }), "mac")).toBe("ctrl+shift+p");
    expect(chordFromKeydown(keydown("Enter", { metaKey: true }), "mac")).toBe("meta+enter");
    expect(chordFromKeydown(keydown("a"), "mac")).toBe("a");
    expect(chordFromKeydown(keydown("F2"), "mac")).toBe("f2");
    expect(chordFromKeydown(keydown("ArrowLeft", { altKey: true }), "mac")).toBe("alt+arrowleft");
  });

  it("spells the separator-unfriendly keys by name", () => {
    expect(chordFromKeydown(keydown(" ", { ctrlKey: true }), "mac")).toBe("ctrl+space");
    expect(chordFromKeydown(keydown("+", { metaKey: true }), "mac")).toBe("meta+plus");
  });

  it("answers null for bare modifiers and unnamed keys", () => {
    expect(chordFromKeydown(keydown("Control", { ctrlKey: true }), "mac")).toBeNull();
    expect(chordFromKeydown(keydown("Shift", { shiftKey: true }), "mac")).toBeNull();
    expect(chordFromKeydown(keydown("MediaPlayPause"), "mac")).toBeNull();
  });

  it("reads a printable key from the physical key while Option is held, which composes a character", () => {
    expect(chordFromKeydown(keydown("˜", { altKey: true, metaKey: true, code: "KeyN" }), "mac")).toBe("alt+meta+n");
    expect(chordFromKeydown(keydown("∑", { altKey: true, code: "KeyW" }), "mac")).toBe("alt+w");
    expect(chordFromKeydown(keydown("¡", { altKey: true, code: "Digit1" }), "mac")).toBe("alt+1");
    expect(chordFromKeydown(keydown("‘", { altKey: true, metaKey: true, code: "BracketRight" }), "mac")).toBe(
      "alt+meta+]",
    );
    expect(chordFromKeydown(keydown("“", { altKey: true, metaKey: true, code: "BracketLeft" }), "mac")).toBe(
      "alt+meta+[",
    );
    expect(chordFromKeydown(keydown("÷", { altKey: true, code: "Slash" }), "mac")).toBe("alt+/");
    expect(chordFromKeydown(keydown("n", { metaKey: true, code: "KeyN" }), "mac")).toBe("meta+n");
  });

  it("spells a shifted key under Option as the character Shift types, as it does without Option", () => {
    expect(chordFromKeydown(keydown("#", { shiftKey: true, code: "Digit3" }), "mac")).toBe("shift+#");
    expect(chordFromKeydown(keydown("‹", { altKey: true, shiftKey: true, code: "Digit3" }), "mac")).toBe(
      "alt+shift+#",
    );
    expect(chordFromKeydown(keydown("±", { altKey: true, shiftKey: true, code: "Equal" }), "mac")).toBe(
      "alt+shift+plus",
    );
    expect(chordFromKeydown(keydown("Á", { altKey: true, shiftKey: true, code: "KeyY" }), "mac")).toBe("alt+shift+y");
  });

  it("reads the Space bar from the physical key, and refuses any other whitespace", () => {
    expect(chordFromKeydown(keydown("\u00a0", { altKey: true, code: "Space" }), "mac")).toBe("alt+space");
    expect(chordFromKeydown(keydown("\u00a0", { altKey: true }), "mac")).toBeNull();
  });
});

describe("chordFromKeydown off a Mac", () => {
  it("spells the Control key as meta, the primary modifier", () => {
    expect(chordFromKeydown(keydown("a", { ctrlKey: true }), "windows")).toBe("meta+a");
    expect(chordFromKeydown(keydown("P", { ctrlKey: true, shiftKey: true }), "linux")).toBe("shift+meta+p");
    expect(chordFromKeydown(keydown("F4", { altKey: true }), "windows")).toBe("alt+f4");
  });

  it("never spells ctrl, and treats the Windows key as no chord", () => {
    expect(chordFromKeydown(keydown("a", { metaKey: true }), "windows")).toBeNull();
    expect(chordFromKeydown(keydown("a", { ctrlKey: true, metaKey: true }), "linux")).toBeNull();
    expect(chordFromKeydown(keydown("n", { ctrlKey: true, altKey: true, code: "KeyN" }), "windows")).toBe(
      "alt+meta+n",
    );
  });
});

describe("detectChordPlatform", () => {
  it("reads the platform off what the browser says about itself", () => {
    expect(detectChordPlatform({ platform: "MacIntel", userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X)" })).toBe(
      "mac",
    );
    expect(detectChordPlatform({ platform: "Win32", userAgent: "Mozilla/5.0 (Windows NT 10.0)" })).toBe("windows");
    expect(detectChordPlatform({ platform: "Linux x86_64", userAgent: "Mozilla/5.0 (X11; Linux x86_64)" })).toBe(
      "linux",
    );
  });
});

describe("isRefusedChord", () => {
  it("refuses Ctrl+Alt+Del in its canonical spelling and nothing else", () => {
    expect(isRefusedChord("alt+meta+delete")).toBe(true);
    expect(isRefusedChord("meta+delete")).toBe(false);
    expect(isRefusedChord("alt+meta+backspace")).toBe(false);
  });
});

describe("isWorkspaceWideChord", () => {
  it("wants a modifier other than Shift alone, or a function key", () => {
    expect(isWorkspaceWideChord("meta+k")).toBe(true);
    expect(isWorkspaceWideChord("alt+shift+k")).toBe(true);
    expect(isWorkspaceWideChord("f6")).toBe(true);
    expect(isWorkspaceWideChord("shift+f12")).toBe(true);
    expect(isWorkspaceWideChord("k")).toBe(false);
    expect(isWorkspaceWideChord("shift+k")).toBe(false);
    expect(isWorkspaceWideChord("enter")).toBe(false);
  });
});

describe("isChordHiddenOn", () => {
  it("hides a chord holding the Mac's Control key off a Mac, and nothing else", () => {
    expect(isChordHiddenOn("ctrl+k", "windows")).toBe(true);
    expect(isChordHiddenOn("ctrl+meta+f", "linux")).toBe(true);
    expect(isChordHiddenOn("ctrl+k", "mac")).toBe(false);
    expect(isChordHiddenOn("meta+k", "windows")).toBe(false);
    // A chord browsers keep is warned about, never hidden.
    expect(isChordHiddenOn("meta+w", "mac")).toBe(false);
  });
});

describe("chordNotices", () => {
  it("warns about a chord browsers keep for themselves, wherever the workspace runs", () => {
    expect(chordNotices("meta+w", "mac")).toEqual([
      "⌘W is a shortcut browsers keep for themselves, so this may not work when the workspace is open in a browser.",
    ]);
    expect(chordNotices("meta+tab", "windows")).toEqual([
      "Ctrl+Tab is a shortcut browsers keep for themselves, so this may not work when the workspace is open in a browser.",
    ]);
  });

  it("warns about a chord the OS keeps, with the Mac's remedy", () => {
    expect(chordNotices("meta+tab", "mac")).toEqual([
      "macOS uses ⌘⇥ for the app switcher, so this may never reach the workspace unless you change that in System Settings.",
    ]);
    expect(chordNotices("alt+tab", "windows")).toEqual([
      "Windows uses Alt+Tab for the window switcher, so this may never reach the workspace.",
    ]);
  });

  it("warns about a key Imbue Studio takes from the OS", () => {
    expect(chordNotices("alt+f4", "windows")).toEqual([
      "On Windows, Alt+F4 is normally used for closing the window; in Imbue Studio it runs this action instead.",
    ]);
    expect(chordNotices("alt+f4", "mac")).toEqual([]);
    expect(chordNotices("alt+meta+w", "mac")).toEqual([]);
  });
});

describe("chordKeycapLabels", () => {
  it("draws the modifiers as Apple's symbols in the grammar's order on an Apple platform", () => {
    expect(chordKeycapLabels("ctrl+alt+shift+meta+n", true)).toEqual(["⌃", "⌥", "⇧", "⌘", "N"]);
    expect(chordKeycapLabels("meta+enter", true)).toEqual(["⌘", "↩"]);
    expect(chordKeycapLabels("meta+backspace", true)).toEqual(["⌘", "⌫"]);
  });

  it("spells the modifiers out elsewhere, where meta is the Control key", () => {
    expect(chordKeycapLabels("shift+meta+n", false)).toEqual(["Shift", "Ctrl", "N"]);
    expect(chordKeycapLabels("meta+enter", false)).toEqual(["Ctrl", "Enter"]);
    expect(chordKeycapLabels("meta+backspace", false)).toEqual(["Ctrl", "Backspace"]);
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
    const chords = ["ctrl+enter", "meta+enter"];
    expect(keydownMatchesChords(keydown("Enter", { ctrlKey: true }), chords, "mac")).toBe(true);
    expect(keydownMatchesChords(keydown("Enter", { shiftKey: true }), chords, "mac")).toBe(false);
    expect(keydownMatchesChords(keydown("Enter"), [], "mac")).toBe(false);
  });

  it("matches meta with the Control key off a Mac, where a ctrl chord never matches", () => {
    expect(keydownMatchesChords(keydown("Enter", { ctrlKey: true }), ["meta+enter"], "windows")).toBe(true);
    expect(keydownMatchesChords(keydown("Enter", { ctrlKey: true }), ["ctrl+enter"], "windows")).toBe(false);
    expect(keydownMatchesChords(keydown("Enter", { metaKey: true }), ["meta+enter"], "linux")).toBe(false);
  });

  it("matches the named action only once the resolved chords are loaded", () => {
    const resolved = { app: "chat", chords_by_action: { "send-message": ["enter"] } };
    expect(keydownMatchesAction(keydown("Enter"), resolved, "send-message", "mac")).toBe(true);
    expect(keydownMatchesAction(keydown("Enter"), resolved, "unknown-action", "mac")).toBe(false);
    expect(keydownMatchesAction(keydown("Enter"), null, "send-message", "mac")).toBe(false);
  });
});

describe("readsChordAsApple", () => {
  it("reads every chord with Apple's symbols on a Mac, and a Mac's Control chord so everywhere", () => {
    expect(readsChordAsApple("meta+k", "mac")).toBe(true);
    expect(readsChordAsApple("meta+k", "windows")).toBe(false);
    expect(readsChordAsApple("ctrl+k", "windows")).toBe(true);
  });
});
