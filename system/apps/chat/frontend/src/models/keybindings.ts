/**
 * The resolved chords of the chat's actions, from this app's own backend. Fetched once when the
 * page connects and again whenever the shell announces a keybindings change. Until the first
 * answer lands, the caller's fallback chords (the manifest's defaults) answer, so the composer
 * is never briefly dead.
 *
 * Chords are read where the workspace runs, which the shell says (``onChordSurface``): a page in a
 * browser never runs a chord the browser keeps for itself, even one the browser delivers anyway.
 * Until the shell says, the page reads as a browser, as an unframed page is.
 */

import {
  type ChordContext,
  type ChordSurface,
  chordFromKeydown,
  detectChordPlatform,
  isChordHiddenIn,
  keydownMatchesChords,
  type ResolvedKeybindings,
} from "@imbue/workspace-ui/src/keybindings";

let resolved: ResolvedKeybindings | null = null;
let surface: ChordSurface = "browser";

/** The chat's own shortcuts beyond the composer's send, with the manifest's defaults until the backend answers. */
export const CHAT_SHORTCUT_DEFAULTS = {
  "new-chat": ["alt+meta+n"],
  "stop-agent": ["meta+."],
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

/** The shell said whether the workspace runs in the desktop app or a browser. */
export function setKeybindingSurface(value: ChordSurface): void {
  surface = value;
}

function keybindingContext(): ChordContext {
  return { platform: detectChordPlatform(), surface };
}

export function matchesKeybinding(event: KeyboardEvent, actionId: string, fallbackChords: readonly string[]): boolean {
  const chords = resolved?.chords_by_action[actionId] ?? fallbackChords;
  return keydownMatchesChords(event, chords, keybindingContext());
}

/** The chat shortcut a chord runs, or null. */
export function chatShortcutOfChord(chord: string): ChatShortcut | null {
  if (isChordHiddenIn(chord, keybindingContext())) return null;
  const chordsOf = (shortcut: ChatShortcut): readonly string[] =>
    resolved?.chords_by_action[shortcut] ?? CHAT_SHORTCUT_DEFAULTS[shortcut];
  return CHAT_SHORTCUTS.find((shortcut) => chordsOf(shortcut).includes(chord)) ?? null;
}

/** The chat shortcut a keystroke runs, or null. */
function chatShortcutOfKeydown(event: KeyboardEvent): ChatShortcut | null {
  const chord = chordFromKeydown(event, keybindingContext().platform);
  return chord === null ? null : chatShortcutOfChord(chord);
}

/** Run the chat shortcuts pressed in this document: a key nothing took first (the app contract takes the
 *  workspace-wide chords) runs its shortcut once per press, and a held key's repeats run nothing more. */
export function listenForChatShortcutKeys(run: (shortcut: ChatShortcut) => void): void {
  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented) return;
    const shortcut = chatShortcutOfKeydown(event);
    if (shortcut === null) return;
    event.preventDefault();
    if (!event.repeat) run(shortcut);
  });
}

/** Run the chat shortcut a chord the desktop app took for this page names (``onChord``), if it names one. */
export function runChatShortcutOfChord(chord: string, run: (shortcut: ChatShortcut) => void): void {
  const shortcut = chatShortcutOfChord(chord);
  if (shortcut !== null) run(shortcut);
}

/** For tests: pretend the backend answered these chords. */
export function setResolvedKeybindingsForTest(value: ResolvedKeybindings | null): void {
  resolved = value;
}
