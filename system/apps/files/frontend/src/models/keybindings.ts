/**
 * The resolved chords of the Files actions, from this app's own backend. Fetched once at
 * bootstrap and again whenever the shell announces a keybindings change; until the first
 * answer lands, no action matches (the arrows and type-ahead are built in and unaffected).
 *
 * Chords are read where the workspace runs, which the shell says (``onChordSurface``): a page in a
 * browser never runs a chord the browser keeps for itself, even one the browser delivers anyway.
 * Until the shell says, the page reads as a browser, as an unframed page is.
 */

import {
  type ChordSurface,
  detectChordPlatform,
  keydownMatchesAction,
  type ResolvedKeybindings,
} from "@imbue/workspace-ui/src/keybindings";

let resolved: ResolvedKeybindings | null = null;
let surface: ChordSurface = "browser";

export async function refreshKeybindings(): Promise<void> {
  try {
    const response = await fetch("/__files/api/keybindings/me");
    if (!response.ok) return;
    resolved = (await response.json()) as ResolvedKeybindings;
  } catch {
    // The chords stay as last loaded; the next change announcement retries.
  }
}

/** The shell said whether the workspace runs in the desktop app or a browser. */
export function setKeybindingSurface(value: ChordSurface): void {
  surface = value;
}

export function matchesKeybinding(event: KeyboardEvent, actionId: string): boolean {
  return keydownMatchesAction(event, resolved, actionId, { platform: detectChordPlatform(), surface });
}

/** For tests: pretend the backend answered these chords. */
export function setResolvedKeybindingsForTest(value: ResolvedKeybindings | null): void {
  resolved = value;
}
