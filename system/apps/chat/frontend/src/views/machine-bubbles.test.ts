// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { setParticipantsForTesting } from "../models/Participants";
import { machineBubbles } from "./machine-bubbles";

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

  it("draws the whole reply as one bubble under the workspace agent's face and name when headed", () => {
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
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0].className).toContain("rounded-tl-sm");
  });

  it("keeps an unheaded bubble's top-left corner round, where no header points at it", () => {
    const bubbles = render(machineBubbles({ ...attrs, isHeaded: false })).querySelectorAll("[data-machine-bubble]");
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0].className).not.toContain("rounded-tl-sm");
  });

  it("draws nothing for whitespace-only text", () => {
    expect(machineBubbles({ ...attrs, text: "\n", isHeaded: true })).toBeNull();
    expect(machineBubbles({ ...attrs, text: " \n\n ", isHeaded: true })).toBeNull();
  });

  it("wears no header past the run's first group, and a monogram before the roster has landed", () => {
    expect(render(machineBubbles({ ...attrs, isHeaded: false })).querySelector("[data-machine-header]")).toBeNull();

    const header = render(machineBubbles({ ...attrs, isHeaded: true })).querySelector("[data-machine-header]");
    expect(header?.textContent).toBe("AAgent");
    expect(header?.querySelector("img")).toBeNull();
  });
});
