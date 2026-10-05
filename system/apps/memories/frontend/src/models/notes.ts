/**
 * The page's data: the backend's ``GET /api/notes`` document, read when the window is shown, and the corrections
 * the page can make. Each write carries the version the page read, so a note a chat changed meanwhile is refused
 * rather than overwritten.
 */

import m from "mithril";

export type NoteType = "USER" | "FEEDBACK" | "PROJECT" | "REFERENCE" | "OTHER";

/** CHAT: a live chat, by title. NOT_A_CHAT: an agent no live chat holds (a deleted chat or a background task).
 *  UNKNOWN: the chat app could not be asked, or the session belongs to no agent mngr knows. */
export type AuthorKind = "CHAT" | "NOT_A_CHAT" | "UNKNOWN";

export interface NoteAuthor {
  readonly kind: AuthorKind;
  readonly chat_title: string | null;
  readonly at: string | null;
}

export interface NoteAttribution {
  readonly authors: readonly NoteAuthor[];
  readonly reader_count: number;
}

export interface Note {
  readonly file_name: string;
  readonly name: string | null;
  readonly description: string;
  readonly note_type: NoteType;
  readonly body: string;
  readonly raw_text: string;
  readonly modified_at: string;
  readonly version: string;
  readonly attribution: NoteAttribution | null;
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
  readonly backups: BackupRetention;
  readonly notes: readonly Note[];
  readonly messages: readonly string[];
}

export type NotesState =
  | { readonly kind: "loading" }
  | { readonly kind: "loaded"; readonly document: NotesDocument }
  | { readonly kind: "failed"; readonly message: string };

const NOTES_PATH = "/api/notes";

const NOTE_CONFLICT = "A chat changed this note while you had it open, so nothing was changed.";
const UNREACHABLE = "Couldn't reach Agent Memory. Try again in a moment.";
// The shell answers 503 while it wakes the app.
const STARTING = "Agent Memory is starting. Try again in a moment.";

let state: NotesState = { kind: "loading" };
// Reads can finish out of order (a focus refresh racing the one after a write); only the latest one started counts.
let latestRefresh = 0;

export function getNotesState(): NotesState {
  return state;
}

/** Why a request was refused: the server's own reason when it gives one. */
async function refusal(response: Response): Promise<string> {
  if (response.status === 409) return NOTE_CONFLICT;
  const answer: unknown = await response.json().catch(() => null);
  const detail = typeof answer === "object" && answer !== null ? (answer as { detail?: unknown }).detail : undefined;
  if (typeof detail === "string") return detail;
  return response.status === 503 ? STARTING : `The page answered ${response.status}.`;
}

export async function refreshNotes(): Promise<void> {
  const refresh = ++latestRefresh;
  try {
    const response = await fetch(NOTES_PATH, { cache: "no-store" });
    const next: NotesState = response.ok
      ? { kind: "loaded", document: (await response.json()) as NotesDocument }
      : { kind: "failed", message: await refusal(response) };
    if (refresh !== latestRefresh) return;
    // A failed read keeps the notes already shown.
    if (next.kind === "loaded" || state.kind !== "loaded") state = next;
  } catch {
    if (refresh === latestRefresh && state.kind !== "loaded") state = { kind: "failed", message: UNREACHABLE };
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
    return response.ok ? null : await refusal(response);
  } catch {
    return UNREACHABLE;
  } finally {
    await refreshNotes();
  }
}

/** Save a correction made to ``version`` of the note: the server refuses it if the note has changed since. */
export function saveNote(
  fileName: string,
  description: string,
  body: string,
  version: string,
): Promise<string | null> {
  return send("PUT", `${NOTES_PATH}/${encodeURIComponent(fileName)}`, { description, body, version });
}

export function deleteNote(note: Note): Promise<string | null> {
  return send("DELETE", `${NOTES_PATH}/${encodeURIComponent(note.file_name)}`, { version: note.version });
}
