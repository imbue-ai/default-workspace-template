import { describe, expect, it } from "vitest";
import { ModifierLatch, XK_CONTROL_L, XK_ESCAPE, XK_SHIFT_L } from "./modifier_latch";

function build() {
  const sent: string[] = [];
  const latch = new ModifierLatch({
    keyDown: (keysym) => sent.push(`down:${keysym.toString(16)}`),
    keyUp: (keysym) => sent.push(`up:${keysym.toString(16)}`),
  });
  return { sent, latch };
}

describe("ModifierLatch", () => {
  it("holds a latched modifier through the next key and releases it after", () => {
    const { sent, latch } = build();
    expect(latch.toggle("ctrl")).toBe(true);
    expect(latch.isLatched("ctrl")).toBe(true);
    latch.tap(0x63);
    expect(sent).toEqual([`down:${XK_CONTROL_L.toString(16)}`, "down:63", "up:63", `up:${XK_CONTROL_L.toString(16)}`]);
    expect(latch.isLatched("ctrl")).toBe(false);
  });

  it("tapping a latched modifier again unlatches it without a key", () => {
    const { sent, latch } = build();
    latch.toggle("shift");
    expect(latch.toggle("shift")).toBe(false);
    expect(sent).toEqual([`down:${XK_SHIFT_L.toString(16)}`, `up:${XK_SHIFT_L.toString(16)}`]);
  });

  it("several latched modifiers are all held and all released", () => {
    const { sent, latch } = build();
    latch.toggle("ctrl");
    latch.toggle("shift");
    latch.tap(XK_ESCAPE);
    expect(sent.filter((entry) => entry.startsWith("up:"))).toHaveLength(3);
    expect(latch.isLatched("ctrl")).toBe(false);
    expect(latch.isLatched("shift")).toBe(false);
  });

  it("a key from the soft keyboard spends the latch when it goes up", () => {
    const { sent, latch } = build();
    latch.toggle("alt");
    latch.keyDown(0x61);
    expect(sent[sent.length - 1]).toBe("down:61");
    latch.keyUp(0x61);
    expect(sent.slice(-2)).toEqual(["up:61", "up:ffe9"]);
  });
});
