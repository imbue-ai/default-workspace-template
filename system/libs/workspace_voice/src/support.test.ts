import { describe, expect, it } from "vitest";
import {
  DEVICE_STORAGE_KEY,
  ENGINE_STORAGE_KEY,
  describeHost,
  DICTATION_SHORTCUT_CHORD,
  dictationShortcutLabel,
  isApplePlatform,
  isDictationSupported,
  pageDictationOptions,
} from "./support";

const CAPABLE = {
  isSecureContext: true,
  hasMicrophoneApi: true,
  hasAudioWorklet: true,
  hasWebAssembly: true,
  hasWorkers: true,
};

describe("dictation support", () => {
  it("is offered only where every piece is there", () => {
    expect(isDictationSupported(CAPABLE)).toBe(true);
    for (const missing of Object.keys(CAPABLE) as (keyof typeof CAPABLE)[]) {
      expect(isDictationSupported({ ...CAPABLE, [missing]: false })).toBe(false);
    }
  });

  it("reads nothing a window-less environment lacks, and finds it unsupported", () => {
    expect(isDictationSupported(describeHost({}))).toBe(false);
  });

  it("takes the page's engine and device from the browser's overrides, else the defaults", () => {
    const storage = new Map<string, string>();
    const read = { getItem: (key: string) => storage.get(key) ?? null };
    expect(pageDictationOptions(read, "", "https://chat.test/agent-1")).toEqual({
      engineId: "moonshine-base",
      preferredDevice: null,
      assetBase: "https://chat.test/assets/voice/",
    });
    storage.set(ENGINE_STORAGE_KEY, "no-such-engine");
    storage.set(DEVICE_STORAGE_KEY, "wasm");
    expect(pageDictationOptions(read, "/myapp", "https://chat.test/myapp/agent-1")).toEqual({
      engineId: "moonshine-base",
      preferredDevice: "wasm",
      assetBase: "https://chat.test/myapp/assets/voice/",
    });
  });

  it("spells the shortcut for the platform", () => {
    expect(isApplePlatform({ platform: "MacIntel" })).toBe(true);
    expect(
      isApplePlatform({ platform: "", userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)" }),
    ).toBe(true);
    expect(isApplePlatform({ platform: "Win32", userAgent: "Mozilla/5.0 (Windows NT 10.0)" })).toBe(false);
    expect(dictationShortcutLabel(true)).toBe("Cmd+Shift+M");
    expect(dictationShortcutLabel(false)).toBe("Ctrl+Shift+M");
    expect(DICTATION_SHORTCUT_CHORD).toBe("shift+meta+m");
  });
});
