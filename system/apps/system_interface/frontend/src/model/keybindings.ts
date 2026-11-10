/**
 * The resolved chords of the shell's own actions, from its backend. Fetched at boot and again on
 * every `keybindings_changed` broadcast; until the first answer lands, each caller's fallback
 * chords (the manifest's defaults) answer, so the launcher is never briefly dead.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import {
  type ChordContext,
  type ChordKeyEvent,
  type ChordPlatform,
  type ChordSurface,
  detectChordPlatform,
  keydownMatchesChords,
  type ResolvedKeybindings,
} from "@imbue/workspace-ui/src/keybindings";

let resolved: ResolvedKeybindings | null = null;
let platformOverride: ChordPlatform | null = null;
// A browser until the Imbue Studio chrome says it frames the shell (``canPopOut``).
let surface: ChordSurface = "browser";

export async function refreshShellKeybindings(): Promise<void> {
  try {
    const response = await fetch(apiUrl("/api/keybindings/me"));
    if (!response.ok) return;
    resolved = (await response.json()) as ResolvedKeybindings;
  } catch {
    // The chords stay as last loaded; the next change broadcast retries.
  }
}

/** Where the shell reads chords: the browser's platform (unless a test named one), and the desktop app or a
 *  browser as the chrome said. */
export function shellChordContext(): ChordContext {
  return { platform: platformOverride ?? detectChordPlatform(), surface };
}

/** The chrome said whether it is the desktop app. */
export function setShellChordSurface(value: ChordSurface): void {
  surface = value;
}

export function matchesShellKeybinding(
  event: ChordKeyEvent,
  actionId: string,
  fallbackChords: readonly string[],
): boolean {
  const chords = resolved?.chords_by_action[actionId] ?? fallbackChords;
  return keydownMatchesChords(event, chords, shellChordContext());
}

/** For tests: pretend the backend answered these chords. */
export function setResolvedShellKeybindingsForTest(value: ResolvedKeybindings | null): void {
  resolved = value;
}

/** For tests: read chords as this platform would, whatever the test runner's browser says. */
export function setShellChordPlatformForTest(platform: ChordPlatform | null): void {
  platformOverride = platform;
}
