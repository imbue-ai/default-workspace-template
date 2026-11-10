/**
 * The resolved chords of the shell's own actions, from its backend. Fetched at boot and again on
 * every `keybindings_changed` broadcast; until the first answer lands, each caller's fallback
 * chords (the manifest's defaults) answer, so the launcher is never briefly dead.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import {
  type ChordKeyEvent,
  type ChordPlatform,
  detectChordPlatform,
  keydownMatchesChords,
  type ResolvedKeybindings,
} from "@imbue/workspace-ui/src/keybindings";

let resolved: ResolvedKeybindings | null = null;
let platformOverride: ChordPlatform | null = null;

export async function refreshShellKeybindings(): Promise<void> {
  try {
    const response = await fetch(apiUrl("/api/keybindings/me"));
    if (!response.ok) return;
    resolved = (await response.json()) as ResolvedKeybindings;
  } catch {
    // The chords stay as last loaded; the next change broadcast retries.
  }
}

/** The platform the shell reads chords on: the browser's, unless a test named one. */
export function shellChordPlatform(): ChordPlatform {
  return platformOverride ?? detectChordPlatform();
}

export function matchesShellKeybinding(
  event: ChordKeyEvent,
  actionId: string,
  fallbackChords: readonly string[],
): boolean {
  const chords = resolved?.chords_by_action[actionId] ?? fallbackChords;
  return keydownMatchesChords(event, chords, shellChordPlatform());
}

/** For tests: pretend the backend answered these chords. */
export function setResolvedShellKeybindingsForTest(value: ResolvedKeybindings | null): void {
  resolved = value;
}

/** For tests: read chords as this platform would, whatever the test runner's browser says. */
export function setShellChordPlatformForTest(platform: ChordPlatform | null): void {
  platformOverride = platform;
}
