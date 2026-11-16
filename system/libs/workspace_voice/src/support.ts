/**
 * Whether a page can offer dictation at all (the workspace-voice-input spec, D5: an unsupported device or browser
 * hides the mic), and the shortcut as the platform spells it.
 */

import { ENGINES, defaultEngineId } from "./engines";
import type { Device, DeviceClass, EngineId } from "./engines";

/** Where an app's build serves the voice runtime's files, under its assets (see ``runtimeAssets.ts``). */
export const VOICE_ASSETS_PATH = "assets/voice/";

/** The browser-local overrides for side-by-side evaluation: an engine id, and a device to insist on. */
export const ENGINE_STORAGE_KEY = "workspace-voice:engine";
export const DEVICE_STORAGE_KEY = "workspace-voice:device";

export interface DictationHost {
  isSecureContext: boolean;
  hasMicrophoneApi: boolean;
  hasAudioWorklet: boolean;
  hasWebAssembly: boolean;
  hasWorkers: boolean;
}

/** The parts of a page's global the support check reads. */
export interface HostLike {
  isSecureContext?: boolean;
  navigator?: { mediaDevices?: { getUserMedia?: unknown } };
  AudioContext?: unknown;
  AudioWorkletNode?: unknown;
  WebAssembly?: unknown;
  Worker?: unknown;
}

/** What this page's global offers; a test passes its own. Reads nothing that a window-less environment lacks. */
export function describeHost(host: HostLike = globalThis as HostLike): DictationHost {
  return {
    isSecureContext: host.isSecureContext === true,
    hasMicrophoneApi: typeof host.navigator?.mediaDevices?.getUserMedia === "function",
    hasAudioWorklet: typeof host.AudioContext === "function" && typeof host.AudioWorkletNode === "function",
    hasWebAssembly: typeof host.WebAssembly === "object",
    hasWorkers: typeof host.Worker === "function",
  };
}

export interface PageDictationOptions {
  engineId: EngineId;
  preferredDevice: Device | null;
  /** The absolute URL the runtime's files are served under, ending in a slash. */
  assetBase: string;
}

/** The dictation a page runs: the registry's default engine for the page's kind of device unless the browser's
 *  override names a registered one, the device the override insists on, and the runtime's files under the page's
 *  base path. */
export function pageDictationOptions(
  storage: Pick<Storage, "getItem">,
  basePath: string,
  pageUrl: string,
  deviceClass: DeviceClass,
): PageDictationOptions {
  const engineOverride = storage.getItem(ENGINE_STORAGE_KEY);
  const engineId = ENGINES.some((engine) => engine.id === engineOverride)
    ? engineOverride!
    : defaultEngineId(deviceClass);
  const deviceOverride = storage.getItem(DEVICE_STORAGE_KEY);
  const preferredDevice = deviceOverride === "webgpu" || deviceOverride === "wasm" ? deviceOverride : null;
  return { engineId, preferredDevice, assetBase: new URL(`${basePath}/${VOICE_ASSETS_PATH}`, pageUrl).href };
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

/** The chord that starts and stops dictation, in the keybinding grammar (modifiers in its fixed
 *  ctrl/alt/shift/meta order), for the platform. */
export function dictationShortcutChord(isApple: boolean): string {
  return isApple ? "shift+meta+m" : "ctrl+shift+m";
}

/** The chord as the tooltip spells it. */
export function dictationShortcutLabel(isApple: boolean): string {
  return isApple ? "Cmd+Shift+M" : "Ctrl+Shift+M";
}
