import { describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend's answers without a network call;
// redraw is a no-op and apiUrl is identity so URLs are predictable.
const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
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
