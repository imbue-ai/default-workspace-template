/**
 * The resolved chords of the chat's actions, from this app's own backend. Fetched once when the
 * page connects and again whenever the shell announces a keybindings change. Until the first
 * answer lands, the caller's fallback chords (the manifest's defaults) answer, so the composer
 * is never briefly dead.
 */

import { keydownMatchesChords, type ResolvedKeybindings } from "@imbue/workspace-ui/src/keybindings";

let resolved: ResolvedKeybindings | null = null;

export async function refreshKeybindings(): Promise<void> {
  try {
    const response = await fetch("/api/keybindings/me");
    if (!response.ok) return;
    resolved = (await response.json()) as ResolvedKeybindings;
  } catch {
    // The chords stay as last loaded; the next change announcement retries.
  }
}

export function matchesKeybinding(event: KeyboardEvent, actionId: string, fallbackChords: readonly string[]): boolean {
  const chords = resolved?.chords_by_action[actionId] ?? fallbackChords;
  return keydownMatchesChords(event, chords);
}

/** For tests: pretend the backend answered these chords. */
export function setResolvedKeybindingsForTest(value: ResolvedKeybindings | null): void {
  resolved = value;
}
