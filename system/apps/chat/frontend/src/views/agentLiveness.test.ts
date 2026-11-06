import { describe, expect, it } from "vitest";
import { isAgentProcessDead } from "./agentLiveness";

describe("isAgentProcessDead", () => {
  it("treats only positive death as dead, with UNKNOWN as non-evidence", () => {
    expect(isAgentProcessDead("STOPPED")).toBe(true);
    expect(isAgentProcessDead("DONE")).toBe(true);
    expect(isAgentProcessDead("REPLACED")).toBe(true);
    expect(isAgentProcessDead("RUNNING")).toBe(false);
    expect(isAgentProcessDead("RUNNING_UNKNOWN_AGENT_TYPE")).toBe(false);
    expect(isAgentProcessDead("WAITING")).toBe(false);
    // The observer could not look; the agent may be running fine. Anything that ACTS on
    // death (unmounting a terminal iframe) must not act on non-evidence.
    expect(isAgentProcessDead("UNKNOWN")).toBe(false);
  });
});
