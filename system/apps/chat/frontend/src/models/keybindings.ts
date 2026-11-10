/**
 * The resolved chords of the chat's actions, from this app's own backend. Fetched once when the
 * page connects and again whenever the shell announces a keybindings change. Until the first
 * answer lands, the caller's fallback chords (the manifest's defaults) answer, so the composer
 * is never briefly dead.
 */

import { chordFromKeydown, keydownMatchesChords, type ResolvedKeybindings } from "@imbue/workspace-ui/src/keybindings";

let resolved: ResolvedKeybindings | null = null;

/** The chat's own shortcuts beyond the composer's send, with the manifest's defaults until the backend answers. */
export const CHAT_SHORTCUT_DEFAULTS = {
  "new-chat": ["alt+meta+n"],
  "interrupt-agent": ["meta+."],
} as const satisfies Record<string, readonly string[]>;

export type ChatShortcut = keyof typeof CHAT_SHORTCUT_DEFAULTS;

const CHAT_SHORTCUTS = Object.keys(CHAT_SHORTCUT_DEFAULTS) as ChatShortcut[];

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

/** The chat shortcut a chord runs, or null. */
export function chatShortcutOfChord(chord: string): ChatShortcut | null {
  const chordsOf = (shortcut: ChatShortcut): readonly string[] =>
    resolved?.chords_by_action[shortcut] ?? CHAT_SHORTCUT_DEFAULTS[shortcut];
  return CHAT_SHORTCUTS.find((shortcut) => chordsOf(shortcut).includes(chord)) ?? null;
}

/** The chat shortcut a keystroke runs, or null. */
export function chatShortcutOfKeydown(event: KeyboardEvent): ChatShortcut | null {
  const chord = chordFromKeydown(event);
  return chord === null ? null : chatShortcutOfChord(chord);
}

/** For tests: pretend the backend answered these chords. */
export function setResolvedKeybindingsForTest(value: ResolvedKeybindings | null): void {
  resolved = value;
}
