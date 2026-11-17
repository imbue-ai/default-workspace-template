// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { setParticipantsForTesting } from "../models/Participants";
import { renderAgentProse, renderAgentTurn } from "./machine-bubbles";

vi.mock("../markdown", () => ({ MarkdownContent: { view: () => null } }));

afterEach(() => {
  setParticipantsForTesting(null, []);
});

function render(node: m.Children): HTMLElement {
  const root = document.createElement("div");
  m.render(root, node);
  return root;
}

describe("renderAgentTurn", () => {
  it("draws the workspace agent's name over the stretch, its face beside the first piece, and the pieces down one column", () => {
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
    const root = render(
      renderAgentTurn([m("p.prose", { key: "prose" }, "words"), m("div.chips", { key: "chips" }, "chips")]),
    );

    expect(root.querySelector("[data-machine-header]")?.textContent).toBe("Atlas");
    const face = root.querySelector<HTMLElement>(".machine-face");
    expect(face?.querySelector("img")?.getAttribute("src")).toBe("/api/participants/workspace-agent/picture");
    expect(face?.style.width).toBe("24px");
    const column = root.querySelector("[data-agent-stretch]");
    expect([...(column?.children ?? [])].map((child) => child.className)).toEqual([
      expect.stringContaining("agent-rail"),
      "prose",
      "chips",
    ]);
  });

  it("wears a monogram before the roster has landed", () => {
    const root = render(renderAgentTurn([]));

    expect(root.querySelector("[data-machine-header]")?.textContent).toBe("Agent");
    const face = root.querySelector(".machine-face");
    expect(face?.querySelector("img")).toBeNull();
    expect(face?.textContent).toBe("A");
  });
});

describe("renderAgentProse", () => {
  const prose = (text: string, layout: "bubble" | "plain" = "bubble", isStretchHead = false): m.Vnode | null =>
    renderAgentProse({ text, timestamp: "t", expansionKeyPrefix: "e1", layout, isStretchHead });

  it("draws nothing for whitespace-only text", () => {
    expect(prose("\n")).toBeNull();
    expect(prose(" \n\n ")).toBeNull();
    expect(prose("first\n\nsecond")).not.toBeNull();
  });

  it("draws a bubble on the stretch's line, its corner squared toward the face only when it opens the stretch", () => {
    const head = render(prose("words", "bubble", true)).querySelector("[data-machine-bubble]");
    expect(head?.getAttribute("data-rail-node")).toBe("box");
    expect(head?.className).toContain("rounded-tl-[4px]");
    expect(render(prose("words")).querySelector("[data-machine-bubble]")?.className).not.toContain("rounded-tl-[4px]");
  });

  it("draws plain text, off the line, inside a piece that holds it", () => {
    const root = render(prose("words", "plain"));
    expect(root.querySelector("[data-agent-prose]")).not.toBeNull();
    expect(root.querySelector("[data-machine-bubble]")).toBeNull();
    expect(root.querySelector("[data-rail-node]")).toBeNull();
  });
});
