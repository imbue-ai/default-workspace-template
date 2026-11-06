// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { BASE_CSS as baseCss } from "../testing/baseCss";
import { blockBody, readDesignTokens, tokensWithPrefix } from "./tokens";

describe("readDesignTokens over the shared stylesheet", () => {
  const tokens = readDesignTokens(baseCss);
  const colors = tokens.colorGroups.flatMap((group) => group.colors);

  it("lists every --c-* color value of :root exactly once", () => {
    const declared = Array.from(
      blockBody(baseCss, /:root\s*\{/).matchAll(/--(c-[\w-]+)\s*:/g),
      (match) => match[1],
    ).filter((name) => !name.startsWith("c-shadow-"));
    expect(declared.length).toBeGreaterThan(20);
    expect(colors.map((color) => color.variable).sort()).toEqual([...declared].sort());
  });

  it("names each color by its utility and resolves it to the value :root declares", () => {
    const accent = colors.find((color) => color.utility === "bg-accent");
    expect(accent).toMatchObject({ name: "accent", variable: "c-accent", value: "#2f6b4f" });
    expect(colors.find((color) => color.utility === "text-primary")?.variable).toBe("c-text-primary");
    expect(colors.find((color) => color.utility === "border-default")?.variable).toBe("c-border");
    expect(colors.find((color) => color.name === "danger-border")?.role).toBe("border");
  });

  it("groups the colors under the stylesheet's own headers, in its order", () => {
    const labels = tokens.colorGroups.map((group) => group.label);
    expect(labels.slice(0, 4)).toEqual(["text", "borders", "surfaces", "fills"]);
    expect(labels).toContain("status");
  });

  it("reads the scales from :root and the theme block, without the colors", () => {
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
    expect(tokens.colorGroups.map((group) => group.label)).toEqual(["text", "values with no utility"]);
    expect(tokens.colorGroups[1].colors).toEqual([
      { name: "c-orphan", role: "bg", utility: null, variable: "c-orphan", value: "rgb(0 0 0 / 0.5)" },
    ]);
    expect(tokens.colorGroups[0].colors[0].utility).toBe("text-ink");
  });

  it("keeps the plain theme block apart from the inline one", () => {
    expect(tokens.theme).toEqual([{ name: "radius-md", value: "6px" }]);
    expect(tokens.scales).toEqual([{ name: "font-size-body", value: "14px" }]);
  });
});

describe("readDesignTokens over a stylesheet with shadow values in the value layer", () => {
  it("keeps a --c-shadow-* value out of the colors", () => {
    const tokens = readDesignTokens(":root { --c-ink: #111; --c-shadow-raised: 0 1px 2px #000; }");
    expect(tokens.colorGroups.flatMap((group) => group.colors).map((color) => color.variable)).toEqual(["c-ink"]);
    expect(tokens.scales).toEqual([{ name: "c-shadow-raised", value: "0 1px 2px #000" }]);
  });
});
