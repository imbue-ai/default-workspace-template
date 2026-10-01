import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { characterStillSvg } from "./stillFrame";

/** The asset the shell serves for the character, as committed. */
const SERVED_STILL = fileURLToPath(
  new URL("../../../../imbue/system_interface/avatar/assets/imbue-character.svg", import.meta.url),
);

describe("the character's still", () => {
  it("is drawn on the grid every avatar design uses", () => {
    expect(characterStillSvg()).toContain('viewBox="0 0 100 100"');
  });

  it("is one filled path and nothing else", () => {
    const svg = characterStillSvg();
    expect([...svg.matchAll(/<path/g)]).toHaveLength(1);
    // The design vocabulary the shell will parse this back through has no
    // room for a script, a style, or an external reference.
    expect(svg).not.toMatch(/<script|<style|<image|href=|<\?/);
  });

  it("is the same drawing every time it is generated", () => {
    expect(characterStillSvg()).toEqual(characterStillSvg());
  });

  it("is leaning, not square", () => {
    // The resting posture is the whole reason the still is generated from the
    // rig rather than traced from the mark.
    expect(characterStillSvg()).toMatch(/rotate\(\d/);
  });

  it("takes a colour", () => {
    expect(characterStillSvg("#0000ff")).toContain('fill="#0000ff"');
  });

  it("matches the asset the shell serves", () => {
    // Regenerate with the command in stillFrame.ts when the rig changes; a
    // still that has drifted is the chooser previewing a pose the character
    // does not hold.
    expect(readFileSync(SERVED_STILL, "utf8").trim()).toEqual(characterStillSvg().trim());
  });
});
