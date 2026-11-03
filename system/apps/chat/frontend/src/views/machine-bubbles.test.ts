// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { setParticipantsForTesting } from "../models/Participants";
import { AGENT_COLUMN_CLASS, agentColumn, machineBubbles } from "./machine-bubbles";

vi.mock("../markdown", () => ({ MarkdownContent: { view: () => null } }));

afterEach(() => {
  setParticipantsForTesting(null, []);
});

function render(node: m.Children): HTMLElement {
  const root = document.createElement("div");
  m.render(root, node);
  return root;
}

describe("machineBubbles", () => {
  const attrs = { text: "first\n\nsecond", timestamp: "t", expansionKeyPrefix: "e1" };

  it("draws the whole reply as one bubble, squared toward the face only beside it", () => {
    const beside = render(machineBubbles({ ...attrs, isBesideFace: true })).querySelectorAll("[data-machine-bubble]");
    expect(beside).toHaveLength(1);
    expect(beside[0].className).toContain("rounded-tl-[4px]");

    const below = render(machineBubbles({ ...attrs, isBesideFace: false })).querySelector("[data-machine-bubble]");
    expect(below?.className).not.toContain("rounded-tl-[4px]");
  });

  it("draws nothing for whitespace-only text", () => {
    expect(machineBubbles({ ...attrs, text: "\n", isBesideFace: true })).toBeNull();
    expect(machineBubbles({ ...attrs, text: " \n\n ", isBesideFace: true })).toBeNull();
  });
});

describe("agentColumn", () => {
  it("heads the column with the workspace agent's name over it and face beside it", () => {
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
    const root = render(agentColumn(true, m("p.content", "work")));

    expect(root.querySelector("[data-machine-header]")?.textContent).toBe("Atlas");
    expect(root.querySelector(".machine-face img")?.getAttribute("src")).toBe(
      "/api/participants/workspace-agent/picture",
    );
    expect(root.querySelector(".content")?.parentElement?.className).toBe(AGENT_COLUMN_CLASS);
  });

  it("holds the same column with no name or face for a row that continues a stretch", () => {
    const root = render(agentColumn(false, m("p.content", "work")));

    expect(root.querySelector("[data-machine-header]")).toBeNull();
    expect(root.querySelector(".machine-face")).toBeNull();
    expect(root.querySelector(".content")?.parentElement?.className).toBe(AGENT_COLUMN_CLASS);
  });

  it("wears a monogram before the roster has landed", () => {
    const root = render(agentColumn(true, null));

    expect(root.querySelector("[data-machine-header]")?.textContent).toBe("Agent");
    const face = root.querySelector(".machine-face");
    expect(face?.querySelector("img")).toBeNull();
    expect(face?.textContent).toBe("A");
  });
});
