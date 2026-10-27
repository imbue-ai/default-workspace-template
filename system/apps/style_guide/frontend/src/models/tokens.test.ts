// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { BASE_CSS as baseCss } from "../testing/baseCss";
import { blockBody, readDesignTokens, tokensWithPrefix } from "./tokens";

describe("readDesignTokens over the shared stylesheet", () => {
  const tokens = readDesignTokens(baseCss);
  const colours = tokens.colourGroups.flatMap((group) => group.colours);

  it("lists every --c-* colour value of :root exactly once", () => {
    const declared = Array.from(
      blockBody(baseCss, /:root\s*\{/).matchAll(/--(c-[\w-]+)\s*:/g),
      (match) => match[1],
    ).filter((name) => !name.startsWith("c-shadow-"));
    expect(declared.length).toBeGreaterThan(20);
    expect(colours.map((colour) => colour.variable).sort()).toEqual([...declared].sort());
  });

  it("names each colour by its utility and resolves it to the value :root declares", () => {
    const accent = colours.find((colour) => colour.utility === "bg-accent");
    expect(accent).toMatchObject({ name: "accent", variable: "c-accent", value: "#2f6b4f" });
    expect(colours.find((colour) => colour.utility === "text-primary")?.variable).toBe("c-text-primary");
    expect(colours.find((colour) => colour.utility === "border-default")?.variable).toBe("c-border");
    expect(colours.find((colour) => colour.name === "danger-border")?.role).toBe("border");
  });

  it("groups the colours under the stylesheet's own headers, in its order", () => {
    const labels = tokens.colourGroups.map((group) => group.label);
    expect(labels.slice(0, 4)).toEqual(["text", "borders", "surfaces", "fills"]);
    expect(labels).toContain("status");
  });

  it("reads the scales from :root and the theme block, without the colours", () => {
    expect(tokens.scales.filter((token) => token.name.startsWith("c-")).map((token) => token.name)).toEqual([
      "c-shadow-raised",
      "c-shadow-overlay",
    ]);
    expect(tokensWithPrefix(tokens.scales, "font-size-").map((token) => token.name)).toContain("font-size-body");
    expect(tokensWithPrefix(tokens.scales, "dur-").map((token) => token.value)).toEqual(["80ms", "120ms", "200ms"]);
    expect(tokensWithPrefix(tokens.theme, "radius-").map((token) => token.name)).toEqual([
      "radius-sm",
      "radius-md",
      "radius-lg",
      "radius-xl",
    ]);
    expect(tokensWithPrefix(tokens.theme, "font-").map((token) => token.name)).toEqual([
      "font-sans",
      "font-mono",
      "font-display",
    ]);
  });
});

describe("readDesignTokens over a small stylesheet", () => {
  const css = `
    /* :root below holds the values */
    :root {
      --c-ink: #111;
      /* a note */
      --c-orphan: rgb(0 0 0 / 0.5);
      --font-size-body: 14px;
    }
    @theme inline {
      /* text: text-ink */
      --color-ink: var(--c-ink);
    }
    @theme {
      --radius-md: 6px;
    }`;
  const tokens = readDesignTokens(css);

  it("puts a value no utility is registered over in a group of its own, with no utility name", () => {
    expect(tokens.colourGroups.map((group) => group.label)).toEqual(["text", "values with no utility"]);
    expect(tokens.colourGroups[1].colours).toEqual([
      { name: "c-orphan", role: "bg", utility: null, variable: "c-orphan", value: "rgb(0 0 0 / 0.5)" },
    ]);
    expect(tokens.colourGroups[0].colours[0].utility).toBe("text-ink");
  });

  it("keeps the plain theme block apart from the inline one", () => {
    expect(tokens.theme).toEqual([{ name: "radius-md", value: "6px" }]);
    expect(tokens.scales).toEqual([{ name: "font-size-body", value: "14px" }]);
  });
});

describe("readDesignTokens over a stylesheet with shadow values in the value layer", () => {
  it("keeps a --c-shadow-* value out of the colours", () => {
    const tokens = readDesignTokens(":root { --c-ink: #111; --c-shadow-raised: 0 1px 2px #000; }");
    expect(tokens.colourGroups.flatMap((group) => group.colours).map((colour) => colour.variable)).toEqual(["c-ink"]);
    expect(tokens.scales).toEqual([{ name: "c-shadow-raised", value: "0 1px 2px #000" }]);
  });
});
