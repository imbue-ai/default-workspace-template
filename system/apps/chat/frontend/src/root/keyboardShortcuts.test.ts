import { describe, expect, it } from "vitest";
import { chatIdForShortcut, chatShortcut } from "./keyboardShortcuts";

function key(key: string, modifiers: KeyboardEventInit = {}) {
  return chatShortcut({ key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, ...modifiers });
}

describe("chat keyboard shortcuts", () => {
  it("recognizes new chat, numbered chats, and adjacent chats", () => {
    expect(key("n", { ctrlKey: true })).toEqual({ kind: "new" });
    expect(key("n", { metaKey: true })).toEqual({ kind: "new" });
    expect(key("1", { metaKey: true })).toEqual({ kind: "index", index: 0 });
    expect(key("9", { metaKey: true })).toEqual({ kind: "index", index: 8 });
    expect(key("2", { ctrlKey: true })).toEqual({ kind: "index", index: 1 });
    expect(key("[", { metaKey: true, shiftKey: true })).toEqual({ kind: "relative", offset: -1 });
    expect(key("}", { metaKey: true, shiftKey: true })).toEqual({ kind: "relative", offset: 1 });
    expect(key("[", { ctrlKey: true, shiftKey: true })).toEqual({ kind: "relative", offset: -1 });
    expect(key("]", { ctrlKey: true, shiftKey: true })).toEqual({ kind: "relative", offset: 1 });
  });

  it("leaves plain typing and unrelated modifiers alone", () => {
    expect(key("n")).toBeNull();
    expect(key("n", { ctrlKey: true, shiftKey: true })).toBeNull();
    expect(key("n", { metaKey: true, shiftKey: true })).toBeNull();
    expect(key("n", { metaKey: true, ctrlKey: true })).toBeNull();
    expect(key("1", { ctrlKey: true, metaKey: true })).toBeNull();
    expect(key("[", { metaKey: true })).toBeNull();
    expect(key("1", { metaKey: true, altKey: true })).toBeNull();
  });

  it("uses the visible order and wraps at either end", () => {
    const ids = ["first", "second", "third"];
    expect(chatIdForShortcut({ kind: "index", index: 1 }, ids, "first")).toBe("second");
    expect(chatIdForShortcut({ kind: "index", index: 8 }, ids, "first")).toBeNull();
    expect(chatIdForShortcut({ kind: "relative", offset: -1 }, ids, "first")).toBe("third");
    expect(chatIdForShortcut({ kind: "relative", offset: 1 }, ids, "third")).toBe("first");
  });
});
