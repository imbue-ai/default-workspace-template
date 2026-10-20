import { describe, expect, it, vi } from "vitest";

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

describe("the auto-compact store", () => {
  it("reads and writes the chat's whole state at its autocompact endpoint", async () => {
    const autocompact = await freshModule();
    mockRequest.mockResolvedValueOnce({ state: ON });
    expect(await autocompact.ensureAutocompactState("agent-1")).toEqual(ON);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({ method: "GET", url: "/api/chats/:chatId/autocompact", params: { chatId: "agent-1" } }),
    );

    mockRequest.mockResolvedValueOnce({ state: OFF });
    expect(await autocompact.setAutocompactState("agent-1", OFF)).toEqual(OFF);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({
        method: "PUT",
        url: "/api/chats/:chatId/autocompact",
        params: { chatId: "agent-1" },
        body: OFF,
      }),
    );
    expect(autocompact.getAutocompactState("agent-1")).toEqual(OFF);
  });
});

describe("autocompactLabel", () => {
  it("names each position", async () => {
    const autocompact = await freshModule();
    expect(autocompact.autocompactLabel(true)).toBe("On");
    expect(autocompact.autocompactLabel(false)).toBe("Off");
  });
});
