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
