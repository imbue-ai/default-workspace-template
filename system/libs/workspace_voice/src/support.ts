/**
 * Whether a page can offer dictation at all (the workspace-voice-input spec, D5: an unsupported device or browser
 * hides the mic), and the shortcut as the platform spells it.
 */

export interface DictationHost {
  isSecureContext: boolean;
  hasMicrophoneApi: boolean;
  hasAudioWorklet: boolean;
  hasWebAssembly: boolean;
  hasWorkers: boolean;
}

/** What this page's window offers; a test passes its own. */
export function describeHost(window: Window & typeof globalThis): DictationHost {
  return {
    isSecureContext: window.isSecureContext,
    hasMicrophoneApi: typeof window.navigator?.mediaDevices?.getUserMedia === "function",
    hasAudioWorklet: typeof window.AudioContext === "function" && typeof window.AudioWorkletNode === "function",
    hasWebAssembly: typeof window.WebAssembly === "object",
    hasWorkers: typeof window.Worker === "function",
  };
}

/** Whether the mic is offered: the microphone needs a secure context, the detector an audio worklet and
 *  WebAssembly, and the engine a worker. */
export function isDictationSupported(host: DictationHost): boolean {
  return (
    host.isSecureContext && host.hasMicrophoneApi && host.hasAudioWorklet && host.hasWebAssembly && host.hasWorkers
  );
}

/** Whether the platform's command key is Cmd, read off the navigator the way the keybinding dialogs do. */
export function isApplePlatform(navigator: { platform?: string; userAgent?: string }): boolean {
  return (
    /Mac|iPhone|iPad|iPod/.test(navigator.platform ?? "") || /Mac OS X|iPhone|iPad/.test(navigator.userAgent ?? "")
  );
}

/** The chord that starts and stops dictation, in the keybinding grammar, for the platform. */
export function dictationShortcutChord(isApple: boolean): string {
  return isApple ? "meta+shift+m" : "ctrl+shift+m";
}

/** The chord as the tooltip spells it. */
export function dictationShortcutLabel(isApple: boolean): string {
  return isApple ? "Cmd+Shift+M" : "Ctrl+Shift+M";
}
