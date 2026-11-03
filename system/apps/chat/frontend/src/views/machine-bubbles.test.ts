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
  it("draws the workspace agent's name over one bubble holding the turn, and its face beside the bubble", () => {
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
    const root = render(renderAgentTurn([m("p.prose", "words"), m("div.chips", "chips")]));

    expect(root.querySelector("[data-machine-header]")?.textContent).toBe("Atlas");
    const face = root.querySelector<HTMLElement>(".machine-face");
    expect(face?.querySelector("img")?.getAttribute("src")).toBe("/api/participants/workspace-agent/picture");
    expect(face?.style.width).toBe("36px");
    const bubbles = root.querySelectorAll("[data-machine-bubble]");
    expect(bubbles).toHaveLength(1);
    expect([...bubbles[0].children].map((child) => child.className)).toEqual(["prose", "chips"]);
  });

  it("wears a monogram before the roster has landed", () => {
    const root = render(renderAgentTurn(null));

    expect(root.querySelector("[data-machine-header]")?.textContent).toBe("Agent");
    const face = root.querySelector(".machine-face");
    expect(face?.querySelector("img")).toBeNull();
    expect(face?.textContent).toBe("A");
  });
});

describe("renderAgentProse", () => {
  it("draws nothing for whitespace-only text", () => {
    expect(renderAgentProse("\n", "t", "e1")).toBeNull();
    expect(renderAgentProse(" \n\n ", "t", "e1")).toBeNull();
    expect(renderAgentProse("first\n\nsecond", "t", "e1")).not.toBeNull();
  });
});
