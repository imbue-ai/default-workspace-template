/**
 * Short lines other apps in the workspace put into a chat, such as "Claude usage 2%" from AI Usage Limits
 * (the backend's `app_notes.py`).
 *
 * A note is not a transcript event: no agent sees it and it has no place in the transcript's offsets, so it is
 * held beside the transcript and drawn between the rows at the time it arrived. The page reads a chat's notes
 * whenever its stream (re)opens, and each new note arrives on the stream as an `app_note` frame.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export const APP_NOTE_FRAME_TYPE = "app_note";

export interface AppNote {
  note_id: string;
  timestamp: string;
  source: string;
  text: string;
}

export interface AppNoteFrame extends AppNote {
  type: typeof APP_NOTE_FRAME_TYPE;
}

const notesByChat = new Map<string, AppNote[]>();
// Bumped on every change to a chat's notes, so the page's memoized rows rebuild.
const versionByChat = new Map<string, number>();

function bump(chatId: string): void {
  versionByChat.set(chatId, (versionByChat.get(chatId) ?? 0) + 1);
}

export function isAppNoteFrame(raw: { type?: string }): raw is AppNoteFrame {
  return raw.type === APP_NOTE_FRAME_TYPE;
}

/** Hold the frame's note for its chat (once) and redraw. */
export function applyAppNoteFrame(chatId: string, frame: AppNoteFrame): void {
  const held = notesByChat.get(chatId) ?? [];
  if (held.some((note) => note.note_id === frame.note_id)) return;
  const note: AppNote = { note_id: frame.note_id, timestamp: frame.timestamp, source: frame.source, text: frame.text };
  notesByChat.set(chatId, [...held, note]);
  bump(chatId);
  m.redraw();
}

/** Read the chat's notes from the server, replacing what is held, except a held note newer than all of the answer's
 *  (a frame that arrived while the read was in flight). A failed read keeps what is held. */
export async function fetchAppNotes(chatId: string): Promise<void> {
  try {
    const response = await fetch(apiUrl(`/api/chats/${encodeURIComponent(chatId)}/app-notes`));
    if (!response.ok) return;
    const body = (await response.json()) as { notes?: AppNote[] };
    const read = body.notes ?? [];
    const newestRead = Math.max(-Infinity, ...read.map((note) => Date.parse(note.timestamp)));
    const readIds = new Set(read.map((note) => note.note_id));
    const arrivedSince = (notesByChat.get(chatId) ?? []).filter(
      (note) => !readIds.has(note.note_id) && Date.parse(note.timestamp) > newestRead,
    );
    notesByChat.set(chatId, [...read, ...arrivedSince]);
    bump(chatId);
    m.redraw();
  } catch {
    // The next stream (re)open reads them again.
  }
}

export function getAppNotes(chatId: string): readonly AppNote[] {
  return notesByChat.get(chatId) ?? [];
}

export function getAppNotesVersion(chatId: string): number {
  return versionByChat.get(chatId) ?? 0;
}
