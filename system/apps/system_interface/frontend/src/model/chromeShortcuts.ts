/**
 * Imbue Studio's own shortcuts (New window, Close window, Quit, the zoom keys), as the desktop app hands them to the
 * framed keyboard shortcuts page with `minds:chrome-shortcuts` (the keyboard-shortcuts spec in the mngr repo, section
 * 8.3). They live in the account, not the workspace: the page edits them and asks the chrome to save each one
 * (`minds:save-chrome-shortcut`), and the chrome answers with the catalog again, carrying `error` when the save
 * failed.
 */

/** One of Imbue Studio's actions: its id, what it does, and its default and current chords. */
export interface ChromeShortcutAction {
  readonly id: string;
  readonly label: string;
  readonly defaultChords: readonly string[];
  readonly chords: readonly string[];
}

/** The catalog the chrome sent last, with why the save before it failed (null when it did not). */
export interface ChromeShortcuts {
  readonly actions: readonly ChromeShortcutAction[];
  readonly error: string | null;
}

function readChords(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.every((chord) => typeof chord === "string" && chord !== "") ? [...value] : null;
}

function readAction(value: unknown): ChromeShortcutAction | null {
  if (value === null || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const defaultChords = readChords(record.defaultChords);
  const chords = readChords(record.chords);
  if (typeof record.id !== "string" || typeof record.label !== "string") return null;
  if (defaultChords === null || chords === null) return null;
  return { id: record.id, label: record.label, defaultChords, chords };
}

/** The catalog a `minds:chrome-shortcuts` message carries, or null for one not shaped that way. */
export function readChromeShortcuts(message: Record<string, unknown>): ChromeShortcuts | null {
  if (!Array.isArray(message.actions)) return null;
  const actions = message.actions.map(readAction);
  if (actions.some((action) => action === null)) return null;
  const error = message.error;
  if (error !== undefined && error !== null && typeof error !== "string") return null;
  return { actions: actions as ChromeShortcutAction[], error: typeof error === "string" ? error : null };
}
