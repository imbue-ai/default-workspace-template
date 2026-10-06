import { afterEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend's answers without a network call;
// redraw is recorded and apiUrl is identity so URLs are predictable.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: mockRedraw } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

const ON = { is_enabled: true };
const OFF = { is_enabled: false };

async function freshModule(): Promise<typeof import("./Autocompact")> {
  vi.resetModules();
  mockRequest.mockReset();
  mockRedraw.mockClear();
  return import("./Autocompact");
}

describe("ensureAutocompactState", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads a chat's setting once, shares the load, redraws when it lands, and keeps chats apart", async () => {
    const autocompact = await freshModule();
    mockRequest.mockResolvedValueOnce({ state: ON });

    const [first, second] = await Promise.all([
      autocompact.ensureAutocompactState("agent-1"),
      autocompact.ensureAutocompactState("agent-1"),
    ]);

    expect(first).toEqual(ON);
    expect(second).toEqual(ON);
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({ method: "GET", url: "/api/chats/:chatId/autocompact", params: { chatId: "agent-1" } }),
    );
    expect(mockRedraw).toHaveBeenCalledTimes(1);
    expect(autocompact.getAutocompactState("agent-1")).toEqual(ON);
    expect(autocompact.getAutocompactState("agent-2")).toBeNull();
  });

  it("answers null on a failed load and holds off asking again for a while", async () => {
    vi.useFakeTimers();
    const autocompact = await freshModule();
    mockRequest.mockRejectedValueOnce(new Error("503"));

    expect(await autocompact.ensureAutocompactState("agent-1")).toBeNull();
    expect(await autocompact.ensureAutocompactState("agent-1")).toBeNull();
    expect(mockRequest).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(autocompact.RETRY_DELAY_MS);
    mockRequest.mockResolvedValueOnce({ state: OFF });
    expect(await autocompact.ensureAutocompactState("agent-1")).toEqual(OFF);
    expect(mockRequest).toHaveBeenCalledTimes(2);
  });
});

describe("setAutocompactState", () => {
  it("shows the new setting at once, keeps what the backend answers, and puts the old one back on a refusal", async () => {
    const autocompact = await freshModule();
    mockRequest.mockResolvedValueOnce({ state: ON });
    await autocompact.ensureAutocompactState("agent-1");

    mockRequest.mockResolvedValueOnce({ state: OFF });
    const pending = autocompact.setAutocompactState("agent-1", OFF);
    expect(autocompact.getAutocompactState("agent-1")).toEqual(OFF);
    expect(await pending).toEqual(OFF);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({
        method: "PUT",
        url: "/api/chats/:chatId/autocompact",
        params: { chatId: "agent-1" },
        body: OFF,
      }),
    );

    mockRequest.mockRejectedValueOnce(new Error("500"));
    expect(await autocompact.setAutocompactState("agent-1", ON)).toEqual(OFF);
    expect(autocompact.getAutocompactState("agent-1")).toEqual(OFF);
  });

  it("forgets an optimistic write to a chat it had never loaded when the backend refuses it", async () => {
    const autocompact = await freshModule();
    mockRequest.mockRejectedValueOnce(new Error("404"));

    expect(await autocompact.setAutocompactState("agent-1", OFF)).toBeNull();
    expect(autocompact.getAutocompactState("agent-1")).toBeNull();
  });
});

describe("autocompactLabel", () => {
  it("names each position", async () => {
    const autocompact = await freshModule();
    expect(autocompact.autocompactLabel(true)).toBe("On");
    expect(autocompact.autocompactLabel(false)).toBe("Off");
  });
});
