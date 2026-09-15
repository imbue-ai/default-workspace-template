import { describe, expect, it } from "vitest";
import {
  decodePersistedScrollState,
  encodePersistedScrollState,
  scrollStateStorageKey,
  validateRestoredAnchor,
} from "./persistence";
import { FOLLOW_STATE } from "./state";
import type { ScrollPositionState } from "./types";

const userControlled: ScrollPositionState = {
  kind: "USER_CONTROLLED",
  anchor: { rowKey: "evt-99", offsetPx: 17.5 },
};

describe("persistence codec", () => {
  it("round-trips a USER_CONTROLLED state with its approximate event index", () => {
    const restored = decodePersistedScrollState(encodePersistedScrollState(userControlled, 4321));
    expect(restored.state).toEqual(userControlled);
    expect(restored.anchorEventIndex).toBe(4321);
  });

  it("round-trips FOLLOW (and drops any stray index)", () => {
    const restored = decodePersistedScrollState(encodePersistedScrollState(FOLLOW_STATE, 4321));
    expect(restored.state).toBe(FOLLOW_STATE);
    expect(restored.anchorEventIndex).toBe(null);
  });

  it("falls back to FOLLOW on null, garbage, and non-object payloads", () => {
    for (const raw of [null, "", "not json", "42", '"a string"', "null"]) {
      expect(decodePersistedScrollState(raw).state).toBe(FOLLOW_STATE);
    }
  });

  it("falls back to FOLLOW on a version or shape mismatch", () => {
    expect(decodePersistedScrollState('{"version":2,"state":{"kind":"FOLLOW"}}').state).toBe(FOLLOW_STATE);
    expect(decodePersistedScrollState('{"version":1,"state":{"kind":"UNKNOWN"}}').state).toBe(FOLLOW_STATE);
    expect(decodePersistedScrollState('{"version":1,"state":{"kind":"USER_CONTROLLED"}}').state).toBe(FOLLOW_STATE);
    expect(
      decodePersistedScrollState('{"version":1,"state":{"kind":"USER_CONTROLLED","anchor":{"rowKey":""}}}').state,
    ).toBe(FOLLOW_STATE);
    expect(
      decodePersistedScrollState(
        '{"version":1,"state":{"kind":"USER_CONTROLLED","anchor":{"rowKey":"a","offsetPx":"NaN"}}}',
      ).state,
    ).toBe(FOLLOW_STATE);
  });

  it("ignores an invalid anchorEventIndex without dropping the state", () => {
    const restored = decodePersistedScrollState(
      '{"version":1,"state":{"kind":"USER_CONTROLLED","anchor":{"rowKey":"a","offsetPx":3}},"anchorEventIndex":-5}',
    );
    expect(restored.state.kind).toBe("USER_CONTROLLED");
    expect(restored.anchorEventIndex).toBe(null);
  });

  it("keys storage per agent", () => {
    expect(scrollStateStorageKey("agent-1")).not.toBe(scrollStateStorageKey("agent-2"));
  });
});

describe("validateRestoredAnchor", () => {
  it("keeps a USER_CONTROLLED state whose anchor row still exists", () => {
    const restored = { state: userControlled, anchorEventIndex: 10 };
    expect(validateRestoredAnchor(restored, (key) => key === "evt-99")).toBe(userControlled);
  });

  it("downgrades to FOLLOW when the anchor row is gone", () => {
    const restored = { state: userControlled, anchorEventIndex: 10 };
    expect(validateRestoredAnchor(restored, () => false)).toBe(FOLLOW_STATE);
  });

  it("passes FOLLOW through untouched", () => {
    const restored = { state: FOLLOW_STATE, anchorEventIndex: null };
    expect(validateRestoredAnchor(restored, () => false)).toBe(FOLLOW_STATE);
  });
});
