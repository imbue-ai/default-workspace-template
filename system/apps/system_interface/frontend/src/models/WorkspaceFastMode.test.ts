import { describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend responses without a
// real network call. redraw is a no-op; apiUrl is identity so URLs are
// predictable. ModelSettings is mocked to observe the live per-agent change.
const { mockRequest, mockSetFastMode } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockSetFastMode: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("./ModelSettings", () => ({ setFastMode: mockSetFastMode }));

// The decision and the open prompt are module-level state, so each test gets a
// fresh copy of the module rather than inheriting the previous test's answer.
async function loadWorkspaceFastMode(): Promise<typeof import("./WorkspaceFastMode")> {
  vi.resetModules();
  mockRequest.mockReset();
  mockSetFastMode.mockReset();
  return import("./WorkspaceFastMode");
}

interface RequestOptions {
  method: string;
  url: string;
  body?: { enabled: boolean };
}

/** Answer every request with the decision the caller asked to record, as the
 *  backend does, and report the POST bodies it saw. */
function recordRequests(postedEnabled: boolean[]): void {
  mockRequest.mockImplementation((options: RequestOptions) => {
    if (options.method === "POST") {
      postedEnabled.push(options.body!.enabled);
      return Promise.resolve({ fast_mode: options.body!.enabled });
    }
    return Promise.resolve({ fast_mode: null });
  });
}

/** Let the request promise's callbacks run. */
async function flush(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

describe("the fast-mode prompt's owner", () => {
  it("stays with the conversation that raised it", async () => {
    const workspaceFastMode = await loadWorkspaceFastMode();
    // Every mounted ChatPanel re-checks this on every render, so a second chat
    // that also ran out its grace period must not take the prompt over: handing
    // it back and forth would re-render the whole app on every frame, and the
    // answer would land on whichever chat rendered last.
    workspaceFastMode.openFastModePrompt("agent-a");
    workspaceFastMode.openFastModePrompt("agent-b");

    expect(workspaceFastMode.getFastModePromptAgentId()).toBe("agent-a");
  });
});

describe("answering the fast-mode prompt", () => {
  it("switches the asking chat to standard speed and records that for the workspace", async () => {
    const workspaceFastMode = await loadWorkspaceFastMode();
    const postedEnabled: boolean[] = [];
    recordRequests(postedEnabled);

    workspaceFastMode.openFastModePrompt("agent-a");
    // What both buttons-that-aren't-"keep it", the backdrop and Escape all do.
    workspaceFastMode.resolveFastModePrompt(false);
    await flush();

    expect(mockSetFastMode).toHaveBeenCalledWith("agent-a", false);
    expect(postedEnabled).toEqual([false]);
    expect(workspaceFastMode.getWorkspaceFastMode()).toEqual({ fast_mode: false });
    expect(workspaceFastMode.getFastModePromptAgentId()).toBeNull();
  });

  it("leaves the asking chat alone when the user keeps fast mode on", async () => {
    const workspaceFastMode = await loadWorkspaceFastMode();
    const postedEnabled: boolean[] = [];
    recordRequests(postedEnabled);

    workspaceFastMode.openFastModePrompt("agent-a");
    workspaceFastMode.resolveFastModePrompt(true);
    await flush();

    // The chat is already running fast, so there is nothing to send it.
    expect(mockSetFastMode).not.toHaveBeenCalled();
    expect(postedEnabled).toEqual([true]);
    expect(workspaceFastMode.getWorkspaceFastMode()?.fast_mode).toBe(true);
  });

  it("closes the question before the answer reaches the server", async () => {
    const workspaceFastMode = await loadWorkspaceFastMode();
    mockRequest.mockImplementation(() => new Promise(() => {}));

    workspaceFastMode.openFastModePrompt("agent-a");
    workspaceFastMode.resolveFastModePrompt(false);

    // With the POST still in flight the decision already reads as made, so no
    // chat can raise the prompt again in the meantime.
    expect(workspaceFastMode.getWorkspaceFastMode()?.fast_mode).toBe(false);
    expect(workspaceFastMode.getFastModePromptAgentId()).toBeNull();
  });
});

describe("loading the workspace decision", () => {
  it("leaves it unknown when the request fails, so no chat prompts", async () => {
    const workspaceFastMode = await loadWorkspaceFastMode();
    mockRequest.mockRejectedValue(new Error("offline"));
    vi.spyOn(console, "warn").mockImplementation(() => {});

    workspaceFastMode.fetchWorkspaceFastMode();
    await flush();

    // A missing prompt is better than one raised against a decision we never read.
    expect(workspaceFastMode.getWorkspaceFastMode()).toBeNull();
  });
});
