import { describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend's answers without a network call;
// redraw is recorded and apiUrl is identity so URLs are predictable.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: mockRedraw } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

const AUTO = { mode: "auto" as const, is_switched: false };

async function freshModule(): Promise<typeof import("./FastMode")> {
  vi.resetModules();
  mockRequest.mockReset();
  mockRedraw.mockClear();
  return import("./FastMode");
}

describe("the fast-mode store", () => {
  it("reads and writes the chat's whole state at its fast-mode endpoint", async () => {
    const fastMode = await freshModule();
    mockRequest.mockResolvedValueOnce({ state: AUTO });
    expect(await fastMode.ensureFastModeState("agent-1")).toEqual(AUTO);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({ method: "GET", url: "/api/chats/:chatId/fast-mode", params: { chatId: "agent-1" } }),
    );

    const answered = { mode: "on" as const, is_switched: false };
    mockRequest.mockResolvedValueOnce({ state: answered });
    expect(await fastMode.updateFastModeState("agent-1", answered)).toEqual(answered);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({
        method: "PUT",
        url: "/api/chats/:chatId/fast-mode",
        params: { chatId: "agent-1" },
        body: answered,
      }),
    );
    expect(fastMode.getFastModeState("agent-1")).toEqual(answered);
  });
});

describe("fastModeLabel", () => {
  it("names the mode, and says when auto has already switched the chat", async () => {
    const fastMode = await freshModule();
    expect(fastMode.fastModeLabel({ mode: "off", is_switched: false })).toBe("Off");
    expect(fastMode.fastModeLabel({ mode: "on", is_switched: false })).toBe("On");
    expect(fastMode.fastModeLabel({ mode: "auto", is_switched: false })).toBe("Auto");
    expect(fastMode.fastModeLabel({ mode: "auto", is_switched: true })).toBe("Auto (off now)");
  });
});

describe("fastModeDetail", () => {
  it("explains each mode, auto with the limit it runs to", async () => {
    const fastMode = await freshModule();
    expect(fastMode.fastModeDetail("off", 5)).toBe("Standard speed always");
    expect(fastMode.fastModeDetail("on", 5)).toBe("Fast mode always");
    expect(fastMode.fastModeDetail("auto", 1)).toBe("Fast for the first 1 turn, then standard");
    expect(fastMode.fastModeDetail("auto", 4)).toBe("Fast for the first 4 turns, then standard");
  });
});
