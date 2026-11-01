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

    // The person-turn geometry: the name over the bubble, the face in the gutter beside it.
    const header = root.querySelector("[data-machine-header]");
    expect(header?.textContent).toBe("Atlas");
    expect(root.querySelector(".machine-face img")?.getAttribute("src")).toBe(
      "/api/participants/workspace-agent/picture",
    );
    const bubbles = root.querySelectorAll("[data-machine-bubble]");
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0].className).toContain("rounded-tl-[4px]");
  });

  it("keeps an unheaded bubble's top-left corner round and its gutter held by a spacer", () => {
    const root = render(machineBubbles({ ...attrs, isHeaded: false }));
    const bubbles = root.querySelectorAll("[data-machine-bubble]");
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0].className).not.toContain("rounded-tl-[4px]");
    expect(root.querySelector(".machine-face")).toBeNull();
    expect(root.querySelector("span.w-6[aria-hidden]")).not.toBeNull();
  });

  it("draws nothing for whitespace-only text", () => {
    expect(machineBubbles({ ...attrs, text: "\n", isHeaded: true })).toBeNull();
    expect(machineBubbles({ ...attrs, text: " \n\n ", isHeaded: true })).toBeNull();
  });

  it("wears no header past the run's first group, and a monogram before the roster has landed", () => {
    expect(render(machineBubbles({ ...attrs, isHeaded: false })).querySelector("[data-machine-header]")).toBeNull();

    const root = render(machineBubbles({ ...attrs, isHeaded: true }));
    expect(root.querySelector("[data-machine-header]")?.textContent).toBe("Agent");
    const face = root.querySelector(".machine-face");
    expect(face?.querySelector("img")).toBeNull();
    expect(face?.textContent).toBe("A");
  });
});
