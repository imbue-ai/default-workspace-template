// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { setParticipantsForTesting } from "../models/Participants";
import { machineBubbles, splitParagraphs } from "./machine-bubbles";

vi.mock("../markdown", () => ({ MarkdownContent: { view: () => null } }));

afterEach(() => {
  setParticipantsForTesting(null, []);
});

function render(node: m.Children): HTMLElement {
  const root = document.createElement("div");
  m.render(root, node);
  return root;
}

describe("splitParagraphs", () => {
  it("splits on blank lines and drops the empty ones", () => {
    expect(splitParagraphs("one\n\ntwo\nstill two\n\n\n\nthree\n")).toEqual(["one", "two\nstill two", "three"]);
  });

  it("keeps a fenced code block whole whatever blank lines it holds", () => {
    const text = "before\n\n```python\nx = 1\n\ny = 2\n```\n\nafter";
    expect(splitParagraphs(text)).toEqual(["before", "```python\nx = 1\n\ny = 2\n```", "after"]);
  });

  it("closes a fence only on its own marker", () => {
    const text = "```\n~~~ not a closer\n\nstill code\n```\n\nafter";
    expect(splitParagraphs(text)).toEqual(["```\n~~~ not a closer\n\nstill code\n```", "after"]);
  });

  it("yields nothing for whitespace", () => {
    expect(splitParagraphs(" \n\n ")).toEqual([]);
  });

  it("keeps a details block whole, so a toggle is never split from its body", () => {
    const text =
      "**Honest Software:**\n\n<details><summary>is loyal</summary>\n\nIt answers to you.\n\n</details>\n\nafter";
    expect(splitParagraphs(text)).toEqual([
      "**Honest Software:**",
      "<details><summary>is loyal</summary>\n\nIt answers to you.\n\n</details>",
      "after",
    ]);
  });

  it("keeps nested details blocks whole and tolerates a stray closer", () => {
    const nested = "<details><summary>a</summary>\n<details><summary>b</summary>\n\ninner\n\n</details>\n\n</details>";
    expect(splitParagraphs(`${nested}\n\nafter`)).toEqual([nested, "after"]);
    expect(splitParagraphs("</details>\n\nafter")).toEqual(["</details>", "after"]);
  });
});

describe("machineBubbles", () => {
  const attrs = { text: "first\n\nsecond", timestamp: "t", expansionKeyPrefix: "e1" };

  it("draws one bubble per paragraph under the workspace agent's face and name when headed", () => {
    setParticipantsForTesting("owner", [
      {
        key: "workspace-agent",
        kind: "workspace_agent",
        display_name: "Atlas",
        profile_picture_url: "/api/participants/workspace-agent/picture",
        email: null,
        handle: "agent",
        color: "#000",
        is_owner: false,
        is_connected: true,
        last_seen: null,
        last_input_at: null,
      },
    ]);
    const root = render(machineBubbles({ ...attrs, isHeaded: true }));

    const header = root.querySelector("[data-machine-header]");
    expect(header?.textContent).toBe("Atlas");
    expect(header?.querySelector("img")?.getAttribute("src")).toBe("/api/participants/workspace-agent/picture");
    const bubbles = root.querySelectorAll("[data-machine-bubble]");
    expect(bubbles).toHaveLength(2);
    expect(bubbles[0].className).toContain("rounded-tl-sm");
    expect(bubbles[1].className).not.toContain("rounded-tl-sm");
  });

  it("draws nothing for text with no paragraph in it", () => {
    expect(machineBubbles({ ...attrs, text: "\n", isHeaded: true })).toBeNull();
  });

  it("wears no header past the run's first group, and a monogram before the roster has landed", () => {
    expect(render(machineBubbles({ ...attrs, isHeaded: false })).querySelector("[data-machine-header]")).toBeNull();

    const header = render(machineBubbles({ ...attrs, isHeaded: true })).querySelector("[data-machine-header]");
    expect(header?.textContent).toBe("AAgent");
    expect(header?.querySelector("img")).toBeNull();
  });
});
