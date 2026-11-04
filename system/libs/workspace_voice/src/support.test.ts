import { describe, expect, it } from "vitest";
import { dictationShortcutChord, dictationShortcutLabel, isApplePlatform, isDictationSupported } from "./support";

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

  it("spells the shortcut for the platform", () => {
    expect(isApplePlatform({ platform: "MacIntel" })).toBe(true);
    expect(
      isApplePlatform({ platform: "", userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)" }),
    ).toBe(true);
    expect(isApplePlatform({ platform: "Win32", userAgent: "Mozilla/5.0 (Windows NT 10.0)" })).toBe(false);
    expect(dictationShortcutLabel(true)).toBe("Cmd+Shift+M");
    expect(dictationShortcutLabel(false)).toBe("Ctrl+Shift+M");
    expect(dictationShortcutChord(true)).toBe("meta+shift+m");
    expect(dictationShortcutChord(false)).toBe("ctrl+shift+m");
  });
});
