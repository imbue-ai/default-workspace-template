import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RETRY_DELAY_MS, createChatSettingStore } from "./chatSettingStore";

// Capture mithril's request so the test drives the backend's answers without a network call;
// redraw is recorded and apiUrl is identity so URLs are predictable.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: mockRedraw } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

interface Setting {
  level: number;
}

const ONE: Setting = { level: 1 };
const TWO: Setting = { level: 2 };

function freshStore() {
  return createChatSettingStore<Setting>({ endpoint: "level", description: "level" });
}

beforeEach(() => {
  mockRequest.mockReset();
  mockRedraw.mockClear();
});

describe("ensure", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads a chat's setting once, shares the load, redraws when it lands, and keeps chats apart", async () => {
    const store = freshStore();
    mockRequest.mockResolvedValueOnce({ state: ONE });

    const [first, second] = await Promise.all([store.ensure("agent-1"), store.ensure("agent-1")]);

    expect(first).toEqual(ONE);
    expect(second).toEqual(ONE);
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({ method: "GET", url: "/api/chats/:chatId/level", params: { chatId: "agent-1" } }),
    );
    expect(mockRedraw).toHaveBeenCalledTimes(1);
    expect(store.get("agent-1")).toEqual(ONE);
    expect(store.get("agent-2")).toBeNull();
  });

  it("answers from the loaded copy without asking again", async () => {
    const store = freshStore();
    mockRequest.mockResolvedValueOnce({ state: ONE });
    await store.ensure("agent-1");

    expect(await store.ensure("agent-1")).toEqual(ONE);
    expect(mockRequest).toHaveBeenCalledTimes(1);
  });

  it("answers null on a failed load and holds off asking again for a while", async () => {
    vi.useFakeTimers();
    const store = freshStore();
    mockRequest.mockRejectedValueOnce(new Error("503"));

    expect(await store.ensure("agent-1")).toBeNull();
    expect(await store.ensure("agent-1")).toBeNull();
    expect(mockRequest).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(RETRY_DELAY_MS);
    mockRequest.mockResolvedValueOnce({ state: TWO });
    expect(await store.ensure("agent-1")).toEqual(TWO);
    expect(mockRequest).toHaveBeenCalledTimes(2);
  });

  it("keeps each store's chats apart from another store's", async () => {
    const first = freshStore();
    const second = freshStore();
    mockRequest.mockResolvedValueOnce({ state: ONE });
    await first.ensure("agent-1");

    expect(second.get("agent-1")).toBeNull();
  });
});

describe("set", () => {
  it("shows the new setting at once, keeps what the backend answers, and puts the old one back on a refusal", async () => {
    const store = freshStore();
    mockRequest.mockResolvedValueOnce({ state: ONE });
    await store.ensure("agent-1");

    mockRequest.mockResolvedValueOnce({ state: TWO });
    const pending = store.set("agent-1", TWO);
    expect(store.get("agent-1")).toEqual(TWO);
    expect(await pending).toEqual(TWO);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({
        method: "PUT",
        url: "/api/chats/:chatId/level",
        params: { chatId: "agent-1" },
        body: TWO,
      }),
    );

    mockRequest.mockRejectedValueOnce(new Error("500"));
    expect(await store.set("agent-1", ONE)).toEqual(TWO);
    expect(store.get("agent-1")).toEqual(TWO);
  });

  it("keeps the backend's answer over the value it was sent", async () => {
    const store = freshStore();
    mockRequest.mockResolvedValueOnce({ state: ONE });

    expect(await store.set("agent-1", TWO)).toEqual(ONE);
    expect(store.get("agent-1")).toEqual(ONE);
  });

  it("forgets an optimistic write to a chat it had never loaded when the backend refuses it", async () => {
    const store = freshStore();
    mockRequest.mockRejectedValueOnce(new Error("404"));

    expect(await store.set("agent-1", TWO)).toBeNull();
    expect(store.get("agent-1")).toBeNull();
  });
});
