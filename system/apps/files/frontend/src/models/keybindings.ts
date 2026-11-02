/**
 * The resolved chords of the Files actions, from this app's own backend. Fetched once at
 * bootstrap and again whenever the shell announces a keybindings change; until the first
 * answer lands, no action matches (the arrows and type-ahead are built in and unaffected).
 */

import { keydownMatchesAction, type ResolvedKeybindings } from "@imbue/workspace-ui/src/keybindings";

let resolved: ResolvedKeybindings | null = null;

export async function refreshKeybindings(): Promise<void> {
  try {
    const response = await fetch("/__files/api/keybindings/me");
    if (!response.ok) return;
    resolved = (await response.json()) as ResolvedKeybindings;
  } catch {
    // The chords stay as last loaded; the next change announcement retries.
  }
}

export function matchesKeybinding(event: KeyboardEvent, actionId: string): boolean {
  return keydownMatchesAction(event, resolved, actionId);
}

/** For tests: pretend the backend answered these chords. */
export function setResolvedKeybindingsForTest(value: ResolvedKeybindings | null): void {
  resolved = value;
}
