/**
 * What the frontend holds about a harness switch, which is only what the WebSocket cannot say.
 *
 * The switch itself is a backend operation reported on the agent, so these tests are about the
 * two seams around that report: the gap between an accepted click and the first push, and a
 * failure the user has read.
 */
import { describe, expect, it, vi } from "vitest";

const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("./AgentManager", () => ({
  chatIdOfAgent: (agent: { id: string; chat_id?: string }) => agent.chat_id ?? agent.id,
}));

// Accepted switches and read failures are module-level state, so each test loads its own copy
// rather than inheriting the previous one's.
async function loadHarnessSwitch(): Promise<typeof import("./HarnessSwitch")> {
  vi.resetModules();
  mockRequest.mockReset();
  mockRequest.mockResolvedValue({ status: "accepted", operation_id: "op" });
  return import("./HarnessSwitch");
}

/** Let the request promise's callbacks run. */
async function flush(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

const CLAUDE_AGENT = { id: "chat-1", name: "Chat-1", state: "RUNNING", labels: {}, work_dir: null, harness: "claude" };

describe("asking for a harness switch", () => {
  it("posts to the chat's own route with an idempotency key", async () => {
    const harnessSwitch = await loadHarnessSwitch();

    harnessSwitch.requestHarnessSwitch("chat-1", "acct-2", "codex");

    expect(mockRequest).toHaveBeenCalledTimes(1);
    const options = mockRequest.mock.calls[0][0] as { method: string; url: string; body: Record<string, string> };
    expect(options.method).toBe("POST");
    expect(options.url).toBe("/api/chats/chat-1/switch-harness");
    expect(options.body.account_id).toBe("acct-2");
    // A retry of the same click has to carry the same key, so there has to BE one.
    expect(options.body.operation_id).not.toBe("");
  });

  it("reads as preparing until the backend's own report arrives", async () => {
    // The POST returns as soon as the switch is accepted, and the card has to say something
    // in the interval before the first push. Once the backend reports, the backend wins.
    const harnessSwitch = await loadHarnessSwitch();

    harnessSwitch.requestHarnessSwitch("chat-1", "acct-2", "codex");
    expect(harnessSwitch.harnessSwitchFor(CLAUDE_AGENT)?.phase).toBe("preparing");
    expect(harnessSwitch.isSwitchingHarness(CLAUDE_AGENT)).toBe(true);

    const reported = { ...CLAUDE_AGENT, handoff: { phase: "finishing" as const, target_harness: "codex" } };
    expect(harnessSwitch.harnessSwitchFor(reported)?.phase).toBe("finishing");
  });

  it("stops claiming to be busy once the chat is observed on the harness it was moving to", async () => {
    // A switch that completed without this client seeing a single push would otherwise leave
    // the card busy forever: the backend clears the field as its last act, so the absence of a
    // report is what "done" looks like.
    const harnessSwitch = await loadHarnessSwitch();

    harnessSwitch.requestHarnessSwitch("chat-1", "acct-2", "codex");
    const arrived = { ...CLAUDE_AGENT, id: "agent-2", chat_id: "chat-1", harness: "codex" };

    expect(harnessSwitch.harnessSwitchFor(arrived)).toBeNull();
    expect(harnessSwitch.isSwitchingHarness(arrived)).toBe(false);
  });

  it("follows the chat rather than the agent, so a switched chat is one chat", async () => {
    const harnessSwitch = await loadHarnessSwitch();

    harnessSwitch.requestHarnessSwitch("chat-1", "acct-2", "codex");
    // The replacement agent has its own id and still backs chat-1.
    const replacement = { ...CLAUDE_AGENT, id: "agent-2", chat_id: "chat-1" };

    expect(harnessSwitch.harnessSwitchFor(replacement)?.target_harness).toBe("codex");
  });
});

describe("a switch that did not happen", () => {
  it("shows the backend's own refusal verbatim and drops the accepted state", async () => {
    // The refusal says whether waiting would help, which is the whole answer -- so it is shown
    // as written rather than summarised, and the frontend keeps no copy of the rules.
    const harnessSwitch = await loadHarnessSwitch();
    mockRequest.mockRejectedValue({
      code: 409,
      response: { detail: "Wait for the current turn to finish before switching harness" },
    });

    harnessSwitch.requestHarnessSwitch("chat-1", "acct-2", "codex");
    await flush();

    expect(harnessSwitch.harnessSwitchFailureFor(CLAUDE_AGENT)).toBe(
      "Wait for the current turn to finish before switching harness",
    );
    expect(harnessSwitch.isSwitchingHarness(CLAUDE_AGENT)).toBe(false);
  });

  it("reports a failure the backend published, until it is dismissed", async () => {
    // A failed phase stays in the payload until the next switch overwrites it, so dismissal is
    // local: the chat is untouched, and there is nothing to clear but the telling.
    const harnessSwitch = await loadHarnessSwitch();
    const failed = {
      ...CLAUDE_AGENT,
      handoff: { phase: "failed" as const, target_harness: "codex", detail: "Could not start the codex agent" },
    };

    expect(harnessSwitch.harnessSwitchFailureFor(failed)).toBe("Could not start the codex agent");
    harnessSwitch.dismissHarnessSwitchFailure(failed);
    expect(harnessSwitch.harnessSwitchFailureFor(failed)).toBeNull();
  });

  it("still reports a LATER failure of the same chat", async () => {
    // Dismissal is keyed on what the user read, not on the chat: two switches never fail
    // identically by accident, and hiding the second would hide a real one.
    const harnessSwitch = await loadHarnessSwitch();
    const first = { ...CLAUDE_AGENT, handoff: { phase: "failed" as const, target_harness: "codex", detail: "first" } };
    harnessSwitch.dismissHarnessSwitchFailure(first);

    const second = { ...CLAUDE_AGENT, handoff: { phase: "failed" as const, target_harness: "pi", detail: "second" } };
    expect(harnessSwitch.harnessSwitchFailureFor(second)).toBe("second");
  });

  it("does not treat a failed switch as one in flight", async () => {
    const harnessSwitch = await loadHarnessSwitch();
    const failed = {
      ...CLAUDE_AGENT,
      handoff: { phase: "failed" as const, target_harness: "codex", detail: "broke" },
    };

    expect(harnessSwitch.isSwitchingHarness(failed)).toBe(false);
  });
});
