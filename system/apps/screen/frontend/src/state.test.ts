/** What the strip says about who has control, and whether this viewer's session is the controller. */
import { describe, expect, it } from "vitest";

import type { ControlDocument } from "./state";
import { describeController, isOpenToFirstInput, isOwnSessionController, isUnclaimedHandoff } from "./state";

function control(overrides: Partial<ControlDocument>): ControlDocument {
  return {
    controller: "nobody",
    human_pinned: false,
    human_session_id: null,
    human_name: null,
    agent_id: null,
    agent_name: null,
    waiting: [],
    idle_seconds: null,
    idle_release_seconds: null,
    ...overrides,
  };
}

describe("describeController", () => {
  it("names this viewer's own session as You and another human by name", () => {
    const human = control({ controller: "human", human_session_id: "s1", human_name: "Josh" });
    expect(describeController(human, "s1")).toBe("You");
    expect(describeController(human, "s2")).toBe("Josh");
    expect(describeController(human, null)).toBe("Josh");
    expect(describeController(control({ controller: "human", human_session_id: "s1" }), "s2")).toBe("Someone");
  });

  it("names an agent by its display name, else its id", () => {
    expect(describeController(control({ controller: "agent", agent_id: "chat-a", agent_name: "Alice" }), "s1")).toBe(
      "Agent Alice",
    );
    expect(describeController(control({ controller: "agent", agent_id: "chat-a" }), "s1")).toBe("Agent chat-a");
    expect(describeController(control({ controller: "agent" }), "s1")).toBe("Agent");
  });

  it("says Nobody for a free Screen", () => {
    expect(describeController(control({}), "s1")).toBe("Nobody");
  });
});

describe("isOwnSessionController", () => {
  it("is true only for the human session that holds control", () => {
    const human = control({ controller: "human", human_session_id: "s1" });
    expect(isOwnSessionController(human, "s1")).toBe(true);
    expect(isOwnSessionController(human, "s2")).toBe(false);
    expect(isOwnSessionController(human, null)).toBe(false);
    expect(isOwnSessionController(control({ controller: "agent", agent_id: "s1" }), "s1")).toBe(false);
    expect(isOwnSessionController(control({}), "s1")).toBe(false);
  });
});

describe("an unclaimed handoff", () => {
  const handedOff = control({ controller: "human", human_pinned: true });

  it("is the human's with no session, named as handed to the viewer, and open to a first input", () => {
    expect(isUnclaimedHandoff(handedOff)).toBe(true);
    expect(describeController(handedOff, "s1")).toBe("Handed to you");
    expect(isOwnSessionController(handedOff, "s1")).toBe(false);
    expect(isOpenToFirstInput(handedOff)).toBe(true);
  });

  it("ends once a session claims it, and never describes a free or agent-held Screen", () => {
    expect(isUnclaimedHandoff(control({ controller: "human", human_pinned: true, human_session_id: "s1" }))).toBe(
      false,
    );
    expect(isUnclaimedHandoff(control({ controller: "nobody" }))).toBe(false);
    expect(isUnclaimedHandoff(control({ controller: "agent", agent_id: "chat-a" }))).toBe(false);
    expect(isOpenToFirstInput(control({ controller: "nobody" }))).toBe(true);
    expect(isOpenToFirstInput(control({ controller: "agent", agent_id: "chat-a" }))).toBe(false);
    expect(isOpenToFirstInput(control({ controller: "human", human_session_id: "s1" }))).toBe(false);
  });
});
