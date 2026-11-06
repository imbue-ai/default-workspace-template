/** The viewer's URL options: ``strip=off`` hides the control row, and a stripless viewer takes control from another
 *  human session at the first press or key (spec section 11). */
import { describe, expect, it } from "vitest";

import type { ControlDocument } from "./state";
import { isHeldByAnotherSession, parseViewerOptions } from "./viewer_options";

const NOBODY: ControlDocument = {
  controller: "nobody",
  human_pinned: false,
  human_session_id: null,
  human_name: null,
  agent_id: null,
  agent_name: null,
  waiting: [],
  idle_seconds: null,
  idle_release_seconds: null,
};

describe("parseViewerOptions", () => {
  it("hides the strip only for strip=off", () => {
    expect(parseViewerOptions("?strip=off")).toEqual({ isStripShown: false });
    expect(parseViewerOptions("?view=compact&strip=off")).toEqual({ isStripShown: false });
    expect(parseViewerOptions("")).toEqual({ isStripShown: true });
    expect(parseViewerOptions("?strip=on")).toEqual({ isStripShown: true });
    expect(parseViewerOptions("?strip=OFF")).toEqual({ isStripShown: true });
  });
});

describe("isHeldByAnotherSession", () => {
  it("is another human session's control only", () => {
    const human = { ...NOBODY, controller: "human" as const, human_pinned: true, human_name: "Ann" };
    expect(isHeldByAnotherSession({ ...human, human_session_id: "s2" }, "s1")).toBe(true);
    expect(isHeldByAnotherSession({ ...human, human_session_id: "s1" }, "s1")).toBe(false);
    // A handoff nobody claimed, nobody, or an agent: not another session's.
    expect(isHeldByAnotherSession({ ...human, human_session_id: null }, "s1")).toBe(false);
    expect(isHeldByAnotherSession(NOBODY, "s1")).toBe(false);
    expect(isHeldByAnotherSession({ ...NOBODY, controller: "agent", agent_id: "a1" }, "s1")).toBe(false);
    // Before this viewer has a session there is nothing to take control with.
    expect(isHeldByAnotherSession({ ...human, human_session_id: "s2" }, null)).toBe(false);
  });
});
