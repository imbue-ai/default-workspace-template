// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import type { TranscriptEvent } from "../models/Response";
import {
  ActivityIndicator,
  isWorkingActivityState,
  labelForActivityState,
  wakeUpSpinnerDeadline,
} from "./ActivityIndicator";
import { notePermissionResolutions, resetShellPermissionResolutionsForTesting } from "./permission-card";

// The component reads the agent's server-derived state through AgentManager; the
// mock factory is hoisted, so the state it serves lives in a mutable holder.
const agentState: { activity_state: string | null } = { activity_state: null };
vi.mock("../models/AgentManager", () => ({ getAgentById: () => agentState }));

function userMsg(ts: string): TranscriptEvent {
  return { timestamp: ts, type: "user_message", event_id: `u-${ts}`, source: "test", role: "user", content: "hi" };
}

function toolUse(ts: string, toolName: string, callId: string, input: string, caption?: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    model: "test-model",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: toolName, input_chars: input.length, caption_label: caption }],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

function toolResult(ts: string, callId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "test-tool",
    output_chars: "result".length,
    is_error: false,
  };
}

/** The tool result of a permission request this agent filed, carrying the
 *  structured object the backend parsed off the gateway's echo. */
function permissionRequestResult(ts: string, requestId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${requestId}`,
    source: "test",
    tool_call_id: `tc-${requestId}`,
    tool_name: "Bash",
    output_chars: 200,
    is_error: false,
    permission_request: { request_id: requestId, request_type: "predefined" },
  };
}

/** The resolution notice minds injects once the agent is actually told. */
function resolutionNotice(ts: string, requestId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "user_message",
    event_id: `u-${requestId}`,
    source: "test",
    role: "user",
    content: `Your permission request was granted. (resolution: granted, request_id: ${requestId})`,
    display: "permission_resolution",
    resolution: "granted",
    request_id: requestId,
  };
}

describe("labelForActivityState — fixed-label states", () => {
  it("hides the indicator for null state (server has no activity tracking for this agent)", () => {
    expect(labelForActivityState(null, [])).toBe(null);
  });

  it("hides the indicator for undefined state (pre-WS-connect)", () => {
    expect(labelForActivityState(undefined, [])).toBe(null);
  });

  it("hides the indicator for IDLE", () => {
    expect(labelForActivityState("IDLE", [userMsg("2026-04-28T01:00:00Z")])).toBe(null);
  });

  it("hides the indicator for an unknown / future state value", () => {
    expect(labelForActivityState("SOMETHING_NEW", [])).toBe(null);
  });

  it("returns 'Thinking…' for THINKING", () => {
    expect(labelForActivityState("THINKING", [userMsg("2026-04-28T01:00:00Z")])).toBe("Thinking…");
  });
});

describe("labelForActivityState — TOOL_RUNNING caption", () => {
  // The caption is whatever the harness's parser put on the call, so this view
  // renders the same way for every harness -- these two cases differ only in the
  // label the backend supplied.
  it("renders the caption the parser attached to a claude tool call", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"src/midnight.ts"}', "Reading midnight.ts"),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Reading midnight.ts");
  });

  it("renders the caption the parser attached to a codex code-mode exec", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "exec", "tc1", 'await tools.exec_command({"cmd":"ls -la"})', "Running ls -la"),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running ls -la");
  });

  it("picks the most recent unmatched tool call, skipping resolved ones", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"old.ts"}', "Reading old.ts"),
      toolResult("2026-04-28T01:00:02Z", "tc1"),
      toolUse("2026-04-28T01:00:03Z", "Read", "tc2", '{"file_path":"new.ts"}', "Reading new.ts"),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Reading new.ts");
  });

  it("falls back to 'Running tool…' for a call parsed before labels existed", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"old.ts"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running tool…");
  });

  it("falls back to 'Running tool…' when no pending tool call is visible yet (timing race)", () => {
    expect(labelForActivityState("TOOL_RUNNING", [userMsg("2026-04-28T01:00:00Z")])).toBe("Running tool…");
  });
});

describe("wakeUpSpinnerDeadline — the beat between the verdict and the agent", () => {
  const NOW = 1_800_000_000_000;

  beforeEach(() => {
    resetShellPermissionResolutionsForTesting();
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    vi.spyOn(Date, "now").mockReturnValue(NOW);
  });

  afterEach(() => {
    resetShellPermissionResolutionsForTesting();
    vi.restoreAllMocks();
  });

  const resolve = (requestId: string): void => {
    notePermissionResolutions({
      type: "minds:permission-resolutions",
      resolutions: [{ requestId, resolution: "granted" }],
    });
  };

  it("shows nothing when no verdict has arrived at all", () => {
    const events = [permissionRequestResult("2026-04-28T01:00:00Z", "req-1")];
    expect(wakeUpSpinnerDeadline(events, NOW)).toBeNull();
  });

  it("holds the strip for 20s once the shell reports a verdict this agent is waiting on", () => {
    const events = [permissionRequestResult("2026-04-28T01:00:00Z", "req-1")];
    resolve("req-1");
    expect(wakeUpSpinnerDeadline(events, NOW)).toBe(NOW + 20_000);
  });

  it("drops the strip once the window has run out", () => {
    const events = [permissionRequestResult("2026-04-28T01:00:00Z", "req-1")];
    resolve("req-1");
    expect(wakeUpSpinnerDeadline(events, NOW + 19_999)).toBe(NOW + 20_000);
    expect(wakeUpSpinnerDeadline(events, NOW + 20_000)).toBeNull();
  });

  it("drops the strip as soon as the agent has actually been told", () => {
    // The notice landing in the transcript IS the agent hearing the verdict --
    // this is the ordinary way the strip ends, well inside the window.
    resolve("req-1");
    const events = [
      permissionRequestResult("2026-04-28T01:00:00Z", "req-1"),
      resolutionNotice("2026-04-28T01:00:03Z", "req-1"),
    ];
    expect(wakeUpSpinnerDeadline(events, NOW + 3_000)).toBeNull();
  });

  it("stays quiet on a reload whose snapshot re-reports an already-delivered verdict", () => {
    // A rebuilt page is pushed every recent verdict, including ones the agent
    // heard days ago; those must not spin the strip of an idle conversation.
    const events = [
      permissionRequestResult("2026-04-28T01:00:00Z", "req-old"),
      resolutionNotice("2026-04-28T01:00:03Z", "req-old"),
    ];
    resolve("req-old");
    expect(wakeUpSpinnerDeadline(events, NOW)).toBeNull();
  });

  it("ignores a verdict for a request some other panel's agent filed", () => {
    // Verdicts are recorded page-wide, but this agent's transcript never
    // mentions req-elsewhere, so its strip has nothing to wait for.
    const events = [permissionRequestResult("2026-04-28T01:00:00Z", "req-mine")];
    resolve("req-elsewhere");
    expect(wakeUpSpinnerDeadline(events, NOW)).toBeNull();
  });

  it("does not restart the clock when the snapshot re-pushes a verdict it already reported", () => {
    const events = [permissionRequestResult("2026-04-28T01:00:00Z", "req-1")];
    resolve("req-1");
    vi.spyOn(Date, "now").mockReturnValue(NOW + 8_000);
    resolve("req-1");
    expect(wakeUpSpinnerDeadline(events, NOW + 8_000)).toBe(NOW + 20_000);
  });

  it("extends to the later deadline when a second request is resolved during the window", () => {
    const events = [
      permissionRequestResult("2026-04-28T01:00:00Z", "req-1"),
      permissionRequestResult("2026-04-28T01:00:01Z", "req-2"),
    ];
    resolve("req-1");
    vi.spyOn(Date, "now").mockReturnValue(NOW + 4_000);
    resolve("req-2");
    expect(wakeUpSpinnerDeadline(events, NOW + 4_000)).toBe(NOW + 24_000);
  });
});

describe("ActivityIndicator — what the strip actually renders", () => {
  const NOW = 1_800_000_000_000;
  const events = [permissionRequestResult("2026-04-28T01:00:00Z", "req-1")];

  beforeEach(() => {
    resetShellPermissionResolutionsForTesting();
    agentState.activity_state = null;
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    vi.spyOn(Date, "now").mockReturnValue(NOW);
  });

  afterEach(() => {
    resetShellPermissionResolutionsForTesting();
    vi.restoreAllMocks();
  });

  const render = (): m.Vnode | null => {
    const component = ActivityIndicator();
    return component.view({ attrs: { agentId: "agent-1", events } } as unknown as Parameters<
      typeof component.view
    >[0]) as m.Vnode | null;
  };

  /** The strip's caption, dug out of mithril's text vnode. */
  const labelTextOf = (strip: m.Vnode | null): string | null => {
    const children = (strip?.children ?? []) as m.Vnode[];
    const text = (children[1]?.children ?? []) as m.Vnode[];
    const content = text[0]?.children;
    return typeof content === "string" ? content : null;
  };

  const resolveReq1 = (): void => {
    notePermissionResolutions({
      type: "minds:permission-resolutions",
      resolutions: [{ requestId: "req-1", resolution: "granted" }],
    });
  };

  it("renders nothing for an idle agent with no verdict in flight", () => {
    agentState.activity_state = "IDLE";
    expect(render()).toBeNull();
  });

  it("names the permission change while the idle agent has yet to hear the verdict", () => {
    agentState.activity_state = "IDLE";
    resolveReq1();
    const strip = render();
    expect(strip).not.toBeNull();
    expect((strip?.attrs as Record<string, unknown>)["data-state"]).toBe("WAKING");
    expect(labelTextOf(strip)).toBe("Confirming permission changes…");
  });

  it("lets a real turn outrank the wake-up caption", () => {
    // The verdict landed AND the agent is already working: the honest caption
    // wins, so the dot never competes with real activity.
    agentState.activity_state = "THINKING";
    resolveReq1();
    const strip = render();
    expect((strip?.attrs as Record<string, unknown>)["data-state"]).toBe("THINKING");
  });
});

describe("isWorkingActivityState — stop-button visibility gate", () => {
  it("treats THINKING / TOOL_RUNNING as an interruptible turn", () => {
    expect(isWorkingActivityState("THINKING")).toBe(true);
    expect(isWorkingActivityState("TOOL_RUNNING")).toBe(true);
  });

  it("treats IDLE as not working (nothing to interrupt)", () => {
    expect(isWorkingActivityState("IDLE")).toBe(false);
  });

  it("treats null / undefined (no activity tracking) as not working", () => {
    expect(isWorkingActivityState(null)).toBe(false);
    expect(isWorkingActivityState(undefined)).toBe(false);
  });

  it("treats an unknown / future state value as not working", () => {
    expect(isWorkingActivityState("SOMETHING_NEW")).toBe(false);
  });
});
