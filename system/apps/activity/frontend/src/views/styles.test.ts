import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { CONTRAST_PAIRS } from "./styles";

// The token values every workspace frontend shares; read from the file so a change there is checked here too.
const BASE_CSS = readFileSync(
  fileURLToPath(new URL("../../../../../libs/workspace_ui/src/base.css", import.meta.url)),
  "utf8",
);

function tokenHex(name: string): string {
  const match = new RegExp(`--c-${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`).exec(BASE_CSS);
  if (match === null) throw new Error(`base.css declares no hex value for --c-${name}`);
  return match[1];
}

/** The design token each colour utility name stands for (``--color-<utility>: var(--c-<token>)``). */
function tokenByUtility(): Map<string, string> {
  const pairs = [...BASE_CSS.matchAll(/--color-([a-z-]+):\s*var\(--c-([a-z-]+)\)/g)].map(
    (match) => [match[1], match[2]] as const,
  );
  return new Map(pairs);
}

/** Every text colour token the page's sources use, as ``text-<utility>`` classes. */
function textTokensInSources(): Set<string> {
  const sourceDir = fileURLToPath(new URL("..", import.meta.url));
  const sources = [...readdirSync(new URL(".", import.meta.url)).map((name) => `views/${name}`), "index.ts"].filter(
    (path) => path.endsWith(".ts") && !path.endsWith(".test.ts"),
  );
  const tokens = tokenByUtility();
  const used = new Set<string>();
  for (const path of sources) {
    const text = readFileSync(`${sourceDir}/${path}`, "utf8");
    for (const match of text.matchAll(/(?<![\w-])text-([a-z]+(?:-[a-z]+)*)/g)) {
      const token = tokens.get(match[1]);
      if (token !== undefined) used.add(token);
    }
  }
  return used;
}

function relativeLuminance(hex: string): number {
  const channels = [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16) / 255);
  const [red, green, blue] = channels.map((value) =>
    value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrastRatio(foreground: string, background: string): number {
  const [lighter, darker] = [relativeLuminance(foreground), relativeLuminance(background)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

describe("colour contrast", () => {
  it("computes WCAG ratios as the standard defines them", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrastRatio("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
  });

  it("lists every text colour the views use, so none goes unchecked", () => {
    const checked = new Set(CONTRAST_PAIRS.filter((pair) => pair.kind === "text").map((pair) => pair.foreground));
    const unchecked = [...textTokensInSources()].filter((token) => !checked.has(token));
    expect(unchecked).toEqual([]);
  });

  it.each(CONTRAST_PAIRS)("$what meets WCAG AA ($kind)", ({ foreground, background, kind }) => {
    const ratio = contrastRatio(tokenHex(foreground), tokenHex(background));
    expect(ratio).toBeGreaterThanOrEqual(kind === "text" ? 4.5 : 3);
  });
});
