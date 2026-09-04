import { describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend responses without a
// real network call. redraw is a no-op; apiUrl is identity so URLs are
// predictable. ModelSettings is mocked to observe the live per-agent change.
const { mockRequest, mockSetFastMode } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockSetFastMode: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("../../base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("./ModelSettings", () => ({ setFastMode: mockSetFastMode }));

// The open prompt and the answered set are module-level state, so each test gets
// a fresh copy of the module rather than inheriting the previous test's answer.
async function loadFastModePrompt(): Promise<typeof import("./FastModePrompt")> {
  vi.resetModules();
  mockRequest.mockReset();
  mockRequest.mockResolvedValue({ status: "ok" });
  mockSetFastMode.mockReset();
  return import("./FastModePrompt");
}

/** Let the request promise's callbacks run. */
async function flush(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

describe("the fast-mode prompt's owner", () => {
  it("stays with the conversation that raised it", async () => {
    const fastModePrompt = await loadFastModePrompt();
    // Every mounted ChatPanel re-checks this on every render, so a second chat
    // that also ran out its grace period must not take the prompt over: handing
    // it back and forth would re-render the whole app on every frame, and the
    // answer would land on whichever chat rendered last.
    fastModePrompt.openFastModePrompt("agent-a");
    fastModePrompt.openFastModePrompt("agent-b");

    expect(fastModePrompt.getFastModePromptAgentId()).toBe("agent-a");
  });
});

describe("answering the fast-mode prompt", () => {
  it("switches the asking chat to standard speed and latches the answer", async () => {
    const fastModePrompt = await loadFastModePrompt();

    fastModePrompt.openFastModePrompt("agent-a");
    // What both the standard-speed button, the backdrop and Escape all do.
    fastModePrompt.resolveFastModePrompt(false);
    await flush();

    expect(mockSetFastMode).toHaveBeenCalledWith("agent-a", false);
    expect(mockRequest).toHaveBeenCalledWith(
      expect.objectContaining({ method: "POST", url: "/api/agents/agent-a/fast-mode-answered" }),
    );
    expect(fastModePrompt.getFastModePromptAgentId()).toBeNull();
    expect(fastModePrompt.isFastModePromptAnswered("agent-a", {})).toBe(true);
  });

  it("leaves the asking chat alone when the user keeps fast mode on, but still latches", async () => {
    const fastModePrompt = await loadFastModePrompt();

    fastModePrompt.openFastModePrompt("agent-a");
    fastModePrompt.resolveFastModePrompt(true);
    await flush();

    // The chat is already running fast, so there is nothing to send it -- but
    // the question was asked and answered, so it must never fire again.
    expect(mockSetFastMode).not.toHaveBeenCalled();
    expect(fastModePrompt.isFastModePromptAnswered("agent-a", {})).toBe(true);
  });

  it("latches before the answer reaches the server", async () => {
    const fastModePrompt = await loadFastModePrompt();
    mockRequest.mockImplementation(() => new Promise(() => {}));

    fastModePrompt.openFastModePrompt("agent-a");
    fastModePrompt.resolveFastModePrompt(false);

    // With the POST still in flight the agent already reads as answered, so no
    // render can raise the prompt again in the meantime.
    expect(fastModePrompt.getFastModePromptAgentId()).toBeNull();
    expect(fastModePrompt.isFastModePromptAnswered("agent-a", {})).toBe(true);
  });

  it("answers only the agent that was asked", async () => {
    const fastModePrompt = await loadFastModePrompt();

    fastModePrompt.openFastModePrompt("agent-a");
    fastModePrompt.resolveFastModePrompt(true);
    await flush();

    expect(fastModePrompt.isFastModePromptAnswered("agent-b", {})).toBe(false);
  });
});

describe("the durable answered label", () => {
  it("reads the label another session recorded", async () => {
    const fastModePrompt = await loadFastModePrompt();
    const labels = { [fastModePrompt.FAST_MODE_ANSWERED_LABEL]: "true" };
    expect(fastModePrompt.isFastModePromptAnswered("agent-a", labels)).toBe(true);
    expect(fastModePrompt.isFastModePromptAnswered("agent-a", {})).toBe(false);
    expect(fastModePrompt.isFastModePromptAnswered("agent-a", undefined)).toBe(false);
  });
});
