/**
 * The keybinding chord grammar on the browser side: one canonical spelling for a keystroke, matching
 * the `workspace_profile` library's grammar (lowercase tokens joined with `+`, modifiers in the fixed
 * ctrl/alt/shift/meta order, named keys as their lowercased `KeyboardEvent.key` values, the literal
 * `+` spelled `plus`). Apps turn a keydown into a chord with `chordFromKeydown` and look it up in the
 * chords their backend resolved from `/api/keybindings/me`.
 */

/** The resolved chords of one app's actions, as its `/api/keybindings/me` route answers them. */
export interface ResolvedKeybindings {
  app: string;
  chords_by_action: Record<string, string[]>;
}

/** The keys a chord is read from; structurally a `KeyboardEvent`, so tests can pass a plain object. */
export interface ChordKeyEvent {
  key: string;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  metaKey: boolean;
}

const NAMED_KEYS = new Set([
  "enter",
  "escape",
  "tab",
  "space",
  "backspace",
  "delete",
  "insert",
  "home",
  "end",
  "pageup",
  "pagedown",
  "arrowup",
  "arrowdown",
  "arrowleft",
  "arrowright",
  "plus",
  "f1",
  "f2",
  "f3",
  "f4",
  "f5",
  "f6",
  "f7",
  "f8",
  "f9",
  "f10",
  "f11",
  "f12",
]);

/**
 * The canonical chord one keydown spells, or null for a keystroke that is no chord (a bare modifier,
 * or a key the grammar has no name for).
 */
export function chordFromKeydown(event: ChordKeyEvent): string | null {
  const raw = event.key;
  if (raw === "" || raw === "Control" || raw === "Alt" || raw === "Shift" || raw === "Meta") {
    return null;
  }
  let key: string;
  if (raw === " ") key = "space";
  else if (raw === "+") key = "plus";
  else if (raw.length === 1) key = raw.toLowerCase();
  else {
    key = raw.toLowerCase();
    if (!NAMED_KEYS.has(key)) return null;
  }
  const tokens: string[] = [];
  if (event.ctrlKey) tokens.push("ctrl");
  if (event.altKey) tokens.push("alt");
  if (event.shiftKey) tokens.push("shift");
  if (event.metaKey) tokens.push("meta");
  tokens.push(key);
  return tokens.join("+");
}

/** Whether the keystroke spells one of the given chords. */
export function keydownMatchesChords(event: ChordKeyEvent, chords: readonly string[]): boolean {
  if (chords.length === 0) return false;
  const chord = chordFromKeydown(event);
  return chord !== null && chords.includes(chord);
}

/** Whether the keystroke triggers the named action under the resolved chords (none loaded: never). */
export function keydownMatchesAction(
  event: ChordKeyEvent,
  resolved: ResolvedKeybindings | null,
  actionId: string,
): boolean {
  if (resolved === null) return false;
  return keydownMatchesChords(event, resolved.chords_by_action[actionId] ?? []);
}
