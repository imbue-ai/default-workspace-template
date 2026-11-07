import { describe, expect, it } from "vitest";
import { railSegments, type RailNode } from "./agent-rail";

/** A piece on the line, laid out as the chat lays it: bubbles from the column's edge, marks centred 22.5px in. */
function node(kind: RailNode["kind"], top: number, bottom: number, centerX = 22.5): RailNode {
  return { kind, top, bottom, left: kind === "box" ? 0 : centerX - 6.5, centerX };
}

describe("railSegments", () => {
  it("joins each piece to the next through the gap between them, stopping short of each by its air", () => {
    const segments = railSegments([
      node("box", 0, 40),
      node("chips", 52, 72),
      node("step", 90, 106),
      node("point", 124, 137),
      node("box", 149, 189),
    ]);

    expect(segments).toEqual([
      // bubble edge -> 2px above the chip row
      { x: 22, top: 40, height: 10 },
      // 2px under the chip row -> 5.5px above the bullet
      { x: 22, top: 74, height: 10.5 },
      // 5.5px under the bullet -> 4px above the tick
      { x: 22, top: 111.5, height: 8.5 },
      // 4px under the tick -> the next bubble's edge
      { x: 22, top: 141, height: 8 },
    ]);
  });

  it("passes by a mark nested off the line, joining the pieces either side of it", () => {
    const segments = railSegments([node("step", 0, 16), node("chips", 30, 50, 52.5), node("step", 70, 86)]);

    expect(segments).toEqual([{ x: 22, top: 21.5, height: 43 }]);
  });

  it("runs on a bubble's text edge when the stretch holds only bubbles, and draws nothing in a gap too small to see", () => {
    expect(railSegments([node("box", 0, 40), node("box", 52, 90)])).toEqual([{ x: 22, top: 40, height: 12 }]);
    expect(railSegments([node("chips", 0, 20), node("box", 22, 60)])).toEqual([]);
    expect(railSegments([node("box", 0, 40)])).toEqual([]);
    expect(railSegments([])).toEqual([]);
  });
});
