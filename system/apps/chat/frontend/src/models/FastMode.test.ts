import { afterEach, describe, expect, it, vi } from "vitest";

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

describe("ensureFastModeState", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads a chat's mode once, shares the load, redraws when it lands, and keeps chats apart", async () => {
    const fastMode = await freshModule();
    mockRequest.mockResolvedValueOnce({ state: AUTO });

    const [first, second] = await Promise.all([
      fastMode.ensureFastModeState("agent-1"),
      fastMode.ensureFastModeState("agent-1"),
    ]);

    expect(first).toEqual(AUTO);
    expect(second).toEqual(AUTO);
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({ method: "GET", url: "/api/chats/:chatId/fast-mode", params: { chatId: "agent-1" } }),
    );
    expect(mockRedraw).toHaveBeenCalledTimes(1);
    expect(fastMode.getFastModeState("agent-1")).toEqual(AUTO);
    expect(fastMode.getFastModeState("agent-2")).toBeNull();
  });

  it("answers null on a failed load and holds off asking again for a while", async () => {
    vi.useFakeTimers();
    const fastMode = await freshModule();
    mockRequest.mockRejectedValueOnce(new Error("503"));

    expect(await fastMode.ensureFastModeState("agent-1")).toBeNull();
    expect(await fastMode.ensureFastModeState("agent-1")).toBeNull();
    expect(mockRequest).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(fastMode.RETRY_DELAY_MS);
    mockRequest.mockResolvedValueOnce({ state: AUTO });
    expect(await fastMode.ensureFastModeState("agent-1")).toEqual(AUTO);
    expect(mockRequest).toHaveBeenCalledTimes(2);
  });
});

describe("updateFastModeState", () => {
  it("shows the new mode at once, keeps what the backend answers, and puts the old one back on a refusal", async () => {
    const fastMode = await freshModule();
    mockRequest.mockResolvedValueOnce({ state: AUTO });
    await fastMode.ensureFastModeState("agent-1");

    const answered = { mode: "on" as const, is_switched: false };
    mockRequest.mockResolvedValueOnce({ state: answered });
    const pending = fastMode.updateFastModeState("agent-1", answered);
    expect(fastMode.getFastModeState("agent-1")).toEqual(answered);
    expect(await pending).toEqual(answered);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({ method: "PUT", url: "/api/chats/:chatId/fast-mode", body: answered }),
    );

    mockRequest.mockRejectedValueOnce(new Error("500"));
    expect(await fastMode.updateFastModeState("agent-1", { mode: "off", is_switched: false })).toEqual(answered);
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
    expect(fastMode.fastModeDetail("off", 5)).toBe("Standard speed for the whole chat.");
    expect(fastMode.fastModeDetail("on", 5)).toBe("Fast for the whole chat.");
    expect(fastMode.fastModeDetail("auto", 1)).toBe("Fast for the first 1 turn, then standard speed.");
    expect(fastMode.fastModeDetail("auto", 4)).toBe("Fast for the first 4 turns, then standard speed.");
  });
});
