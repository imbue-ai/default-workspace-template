import { afterEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend's answers without a network call;
// redraw is recorded and apiUrl is identity so URLs are predictable.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: mockRedraw } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

const STORED = { fast_mode_turn_limit: 5, is_fast_mode_notice_shown: false };

/** A fresh copy of the module, its settings already loaded as STORED. */
async function loadWithSettings(): Promise<typeof import("./ChatSettings")> {
  vi.resetModules();
  mockRequest.mockReset();
  mockRequest.mockResolvedValueOnce({ settings: STORED });
  const chatSettings = await import("./ChatSettings");
  await chatSettings.ensureChatSettings();
  return chatSettings;
}

describe("ensureChatSettings", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads once, shares the load, and redraws when it lands", async () => {
    vi.resetModules();
    mockRequest.mockReset();
    mockRedraw.mockClear();
    const chatSettings = await import("./ChatSettings");
    mockRequest.mockResolvedValueOnce({ settings: STORED });

    const [first, second] = await Promise.all([chatSettings.ensureChatSettings(), chatSettings.ensureChatSettings()]);

    expect(first).toEqual(STORED);
    expect(second).toEqual(STORED);
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mockRedraw).toHaveBeenCalledTimes(1);
    expect(await chatSettings.ensureChatSettings()).toEqual(STORED);
    expect(mockRequest).toHaveBeenCalledTimes(1);
  });

  it("warns about a failed load, answers the defaults, and asks again only after the retry delay", async () => {
    // The callers ask on every render, so a failure that was retried by the next call would
    // loop a request per frame for as long as the backend is down.
    vi.useFakeTimers();
    vi.resetModules();
    mockRequest.mockReset();
    mockRedraw.mockClear();
    const chatSettings = await import("./ChatSettings");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mockRequest.mockRejectedValueOnce(new Error("503"));

    expect(await chatSettings.ensureChatSettings()).toEqual(chatSettings.DEFAULT_CHAT_SETTINGS);

    expect(warn).toHaveBeenCalledTimes(1);
    expect(mockRedraw).not.toHaveBeenCalled();
    expect(chatSettings.getChatSettings()).toBeNull();
    // Inside the delay: the defaults again, with no request behind them.
    vi.advanceTimersByTime(chatSettings.RETRY_DELAY_MS - 1);
    expect(await chatSettings.ensureChatSettings()).toEqual(chatSettings.DEFAULT_CHAT_SETTINGS);
    expect(mockRequest).toHaveBeenCalledTimes(1);
    // Past it: asked again, and this time it lands.
    vi.advanceTimersByTime(1);
    mockRequest.mockResolvedValueOnce({ settings: STORED });
    expect(await chatSettings.ensureChatSettings()).toEqual(STORED);
    expect(mockRequest).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });
});

describe("updateChatSettings", () => {
  it("shows the new settings at once and keeps what the backend answers", async () => {
    const chatSettings = await loadWithSettings();
    const answered = { fast_mode_turn_limit: 2, is_fast_mode_notice_shown: false };
    mockRequest.mockResolvedValueOnce({ settings: answered });

    const pending = chatSettings.updateChatSettings({ ...STORED, fast_mode_turn_limit: 2 });
    expect(chatSettings.getChatSettings()?.fast_mode_turn_limit).toBe(2);

    expect(await pending).toEqual(answered);
    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({ method: "PUT", url: "/api/settings", body: { ...STORED, fast_mode_turn_limit: 2 } }),
    );
    expect(chatSettings.getChatSettings()).toEqual(answered);
  });

  it("puts the previous settings back when the write fails, without rejecting", async () => {
    const chatSettings = await loadWithSettings();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mockRequest.mockRejectedValueOnce(new Error("503"));

    const result = await chatSettings.updateChatSettings({ ...STORED, fast_mode_turn_limit: 9 });

    expect(result).toEqual(STORED);
    expect(chatSettings.getChatSettings()).toEqual(STORED);
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });
});
