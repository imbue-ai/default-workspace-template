/**
 * The page's data: the backend's ``GET /api/notes`` document, read when the window is shown, and the corrections
 * the page can make. Each write carries the version the page read, so a note a chat changed meanwhile is refused
 * rather than overwritten.
 */

import m from "mithril";

export type NoteType = "USER" | "FEEDBACK" | "PROJECT" | "REFERENCE" | "OTHER";

export interface NoteAuthor {
  readonly chat_title: string | null;
  readonly at: string | null;
}

export interface NoteAttribution {
  readonly authors: readonly NoteAuthor[];
  readonly reader_count: number;
}

/** A note's line in MEMORY.md: what every chat starts with, before it opens the note itself. */
export interface IndexEntry {
  readonly title: string;
  readonly hook: string;
  readonly is_loaded: boolean;
}

/** How much of MEMORY.md chats load, and what it lists that no longer exists. */
export interface IndexSummary {
  readonly line_count: number;
  readonly loaded_line_count: number;
  readonly max_lines: number;
  readonly max_bytes: number;
  readonly missing_files: readonly string[];
}

export interface Note {
  readonly file_name: string;
  readonly name: string | null;
  readonly description: string;
  readonly note_type: NoteType;
  /** The harness that saved it (``claude``, ``pi-coding``), when the note says. */
  readonly source: string | null;
  readonly body: string;
  readonly raw_text: string;
  readonly modified_at: string;
  readonly version: string;
  readonly attribution: NoteAttribution | null;
  readonly index_entry: IndexEntry | null;
}

/** What the workspace's backups keep: a deleted note stays in the snapshots taken before the delete. */
export interface BackupRetention {
  readonly is_backed_up: boolean;
  readonly longest_kept: string | null;
  readonly schedule: readonly string[];
  readonly settings_path: string;
}

export interface NotesDocument {
  readonly notes_dir: string;
  readonly index_path: string;
  readonly index: IndexSummary;
  readonly backups: BackupRetention;
  readonly notes: readonly Note[];
  readonly messages: readonly string[];
}

export type NotesState =
  | { readonly kind: "loading" }
  | { readonly kind: "loaded"; readonly document: NotesDocument }
  | { readonly kind: "failed"; readonly message: string };

export const NOTES_PATH = "/api/notes";

let state: NotesState = { kind: "loading" };

export function getNotesState(): NotesState {
  return state;
}

export async function refreshNotes(): Promise<void> {
  try {
    const response = await fetch(NOTES_PATH, { cache: "no-store" });
    if (!response.ok) {
      // A 503 is the shell's "starting" page while the app wakes up: keep what is shown.
      if (state.kind !== "loaded") state = { kind: "failed", message: `The page answered ${response.status}.` };
      return;
    }
    state = { kind: "loaded", document: (await response.json()) as NotesDocument };
  } catch (error) {
    if (state.kind !== "loaded") state = { kind: "failed", message: String(error) };
  } finally {
    m.redraw();
  }
}

async function send(method: "PUT" | "DELETE", path: string, body: object): Promise<string | null> {
  try {
    const response = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (response.ok) return null;
    if (response.status === 409)
      return "A chat changed this note while you had it open. Its latest version is shown now.";
    const detail = ((await response.json().catch(() => ({}))) as { detail?: string }).detail;
    return detail ?? `The page answered ${response.status}.`;
  } catch (error) {
    return String(error);
  } finally {
    await refreshNotes();
  }
}

export function saveNote(note: Note, description: string, body: string): Promise<string | null> {
  return send("PUT", `${NOTES_PATH}/${encodeURIComponent(note.file_name)}`, {
    description,
    body,
    version: note.version,
  });
}

export function deleteNote(note: Note): Promise<string | null> {
  return send("DELETE", `${NOTES_PATH}/${encodeURIComponent(note.file_name)}`, { version: note.version });
}
