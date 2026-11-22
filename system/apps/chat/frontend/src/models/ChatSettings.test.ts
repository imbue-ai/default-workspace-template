import { afterEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend's answers without a network call;
// redraw is recorded and apiUrl is identity so URLs are predictable.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: mockRedraw } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

const STORED = {
  fast_mode_default: "auto" as const,
  fast_mode_turn_limit: 5,
  is_fast_mode_notice_shown: false,
  autocompact_default: true,
  is_autocompact_notice_shown: false,
};

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
    const answered = { ...STORED, fast_mode_turn_limit: 2 };
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

describe("the suggestion switches", () => {
  it("are unknown before the settings load, and on when the backend does not send them", async () => {
    vi.resetModules();
    mockRequest.mockReset();
    const unloaded = await import("./ChatSettings");
    expect(unloaded.isSuggestionSwitchOn("is_next_step_suggestions_enabled")).toBeNull();

    // STORED names none of the switches, as an older backend's answer does.
    const chatSettings = await loadWithSettings();
    expect(chatSettings.isSuggestionSwitchOn("is_next_step_suggestions_enabled")).toBe(true);
    expect(chatSettings.isSuggestionSwitchOn("is_new_chat_suggestions_enabled")).toBe(true);
  });

  it("change alone through their own route, at once on the page, and keep what the backend answers", async () => {
    const chatSettings = await loadWithSettings();
    const answered = { ...STORED, is_next_step_suggestions_enabled: false, is_new_chat_suggestions_enabled: true };
    mockRequest.mockResolvedValueOnce({ settings: answered });

    const pending = chatSettings.updateSuggestionSwitches({ is_next_step_suggestions_enabled: false });
    expect(chatSettings.isSuggestionSwitchOn("is_next_step_suggestions_enabled")).toBe(false);
    await pending;

    expect(mockRequest).toHaveBeenLastCalledWith({
      method: "PUT",
      url: "/api/settings/suggestions",
      body: { is_next_step_suggestions_enabled: false },
    });
    expect(chatSettings.getChatSettings()).toEqual(answered);
  });

  it("go back to what they were when the write fails", async () => {
    const chatSettings = await loadWithSettings();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mockRequest.mockRejectedValueOnce(new Error("400"));

    await chatSettings.updateSuggestionSwitches({ is_next_step_suggestions_enabled: false });

    expect(chatSettings.isSuggestionSwitchOn("is_next_step_suggestions_enabled")).toBe(true);
    expect(chatSettings.getChatSettings()).toEqual(STORED);
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });

  it("ride every write of the whole settings as they were last read, so that write resets none", async () => {
    const chatSettings = await loadWithSettings();
    const switchedOff = { ...STORED, is_next_step_suggestions_enabled: false };
    mockRequest.mockResolvedValueOnce({ settings: switchedOff });
    await chatSettings.updateSuggestionSwitches({ is_next_step_suggestions_enabled: false });
    const held = chatSettings.getChatSettings();
    if (held === null) throw new Error("the settings were loaded");
    mockRequest.mockResolvedValueOnce({ settings: { ...switchedOff, is_fast_mode_notice_shown: true } });

    await chatSettings.updateChatSettings({ ...held, is_fast_mode_notice_shown: true });

    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({
        method: "PUT",
        url: "/api/settings",
        body: { ...STORED, is_next_step_suggestions_enabled: false, is_fast_mode_notice_shown: true },
      }),
    );
  });
});

describe("rereadChatSettings", () => {
  it("asks nothing of the backend for a page that holds no settings", async () => {
    vi.resetModules();
    mockRequest.mockReset();
    const chatSettings = await import("./ChatSettings");

    chatSettings.rereadChatSettings();

    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("replaces the page's copy with what the backend holds now, and tells the listeners", async () => {
    const chatSettings = await loadWithSettings();
    const listener = vi.fn();
    chatSettings.addChatSettingsUpdatedListener(listener);
    const changed = { ...STORED, is_next_step_suggestions_enabled: false };
    mockRequest.mockResolvedValueOnce({ settings: changed });

    chatSettings.rereadChatSettings();
    expect(listener).not.toHaveBeenCalled();
    await Promise.resolve();
    await Promise.resolve();

    expect(mockRequest).toHaveBeenLastCalledWith({ method: "GET", url: "/api/settings" });
    expect(chatSettings.getChatSettings()).toEqual(changed);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("keeps the copy read before when the read fails", async () => {
    const chatSettings = await loadWithSettings();
    const listener = vi.fn();
    chatSettings.addChatSettingsUpdatedListener(listener);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mockRequest.mockRejectedValueOnce(new Error("503"));

    chatSettings.rereadChatSettings();
    await Promise.resolve();
    await Promise.resolve();

    expect(chatSettings.getChatSettings()).toEqual(STORED);
    expect(listener).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });
});
