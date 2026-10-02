/**
 * The words the page shows for a note: who wrote it and who read it, dates, counts, the note's body split
 * into paragraphs and labeled lines (``**Why:** ...``), what chats load of the list, and what the backups still hold
 * after a delete. Pure, so
 * each is tested on its own; the body is returned as plain text pieces, never HTML, since an agent wrote it.
 */

import type {
  AuthorKind,
  BackupRetention,
  IndexSummary,
  MemoryControls,
  MemoryHarness,
  Note,
  NoteAttribution,
} from "../models/notes";

export interface BodyBlock {
  readonly label: string | null;
  readonly text: string;
}

const LABELED_LINE = /^\*\*(.+?):\*\*\s*(.*)$/;
const WIKI_LINK = /\[\[([^\]]+)\]\]/g;
const BOLD = /\*\*(.+?)\*\*/g;

function plainText(text: string): string {
  return text.replace(WIKI_LINK, (_, slug: string) => `"${slug.replace(/[-_]/g, " ")}"`).replace(BOLD, "$1");
}

export function bodyBlocks(body: string): BodyBlock[] {
  return body
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "")
    .map((line) => {
      const labeled = LABELED_LINE.exec(line);
      return labeled === null
        ? { label: null, text: plainText(line) }
        : { label: labeled[1], text: plainText(labeled[2]) };
    });
}

export function countLabel(count: number, singular: string, plural: string): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

/** "Oct 1" this year, "Oct 1, 2025" before it. */
export function formatDate(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  return date.toLocaleDateString(
    undefined,
    date.getFullYear() === now.getFullYear()
      ? { month: "short", day: "numeric" }
      : { month: "short", day: "numeric", year: "numeric" },
  );
}

const WRITER_BY_KIND: Readonly<Record<AuthorKind, (chatTitle: string | null) => string>> = {
  CHAT: (chatTitle) => `Written by "${chatTitle ?? ""}"`,
  NOT_A_CHAT: () => "Written by a chat that has since been deleted, or a background task",
  UNKNOWN: () => "Written by a chat whose name couldn't be read",
};

/** "Written by "Plan the launch" · read by 2 chats", in whatever part the transcripts can support. */
export function attributionLine(attribution: NoteAttribution | null): string {
  if (attribution === null) return "Who wrote this isn't recorded";
  const latest = attribution.authors[0];
  const writer =
    latest === undefined ? "Who wrote this isn't recorded" : WRITER_BY_KIND[latest.kind](latest.chat_title);
  const others =
    attribution.authors.length > 1
      ? ` and ${countLabel(attribution.authors.length - 1, "other chat", "other chats")}`
      : "";
  const readers =
    attribution.reader_count === 0
      ? "not read since"
      : `read by ${countLabel(attribution.reader_count, "chat", "chats")}`;
  return `${writer}${others} · ${readers}`;
}

const HARNESS_NAMES: Readonly<Record<string, string>> = {
  "pi-coding": "pi",
  codex: "Codex",
  opencode: "OpenCode",
  antigravity: "Antigravity",
};

/** Who saved a note: the chat named by its transcript (Claude's or pi's) when one records the save; otherwise the
 *  harness the note itself names; otherwise that it isn't recorded. */
export function writerLine(note: Note): string {
  const hasWriter = note.attribution !== null && note.attribution.authors.length > 0;
  if (hasWriter || note.source === null || note.source === "claude") return attributionLine(note.attribution);
  return `Saved by a ${HARNESS_NAMES[note.source] ?? note.source} chat`;
}

/** How long the backups keep a copy, or null when nothing outside the workspace keeps one. */
function keptFor(backups: BackupRetention): string | null {
  return backups.is_backed_up ? backups.longest_kept : null;
}

/** The backups row under "Where your notes go": deleted notes stay in the backups. */
export function backupsLine(backups: BackupRetention): string {
  const kept = keptFor(backups);
  return kept === null
    ? "This workspace isn't backed up, so a note you delete is gone for good."
    : `Your workspace's backups also hold these notes, including ones you delete, for up to ${kept}.`;
}

/** The delete confirmation's backup paragraph. */
export function deleteBackupsWarning(backups: BackupRetention): string {
  const kept = keptFor(backups);
  return kept === null
    ? "This workspace isn't backed up, so no other copy is kept."
    : `A copy stays in your workspace's backups until they expire, up to ${kept} from now.`;
}

/** What the page says once a note is deleted. */
export function deletedStatus(backups: BackupRetention): string {
  const kept = keptFor(backups);
  return kept === null ? "Deleted." : `Deleted. A copy stays in your backups for up to ${kept}.`;
}

/** The technical detail behind the backups line: the schedule, and the file it comes from. */
export function backupsDetail(backups: BackupRetention): string {
  if (!backups.is_backed_up) return "No backups are set up for this workspace.";
  if (backups.schedule.length === 0) return `Backups are set up, but ${backups.settings_path} keeps no snapshots.`;
  return (
    `Backups are snapshots of the whole workspace, kept ${backups.schedule.join(", ")} (set in ${backups.settings_path}). ` +
    "Each snapshot holds the notes as they were when it was taken, including notes deleted since."
  );
}

/** What chats load of the index, in a sentence, or null when there are no notes to speak of. */
export function indexSummaryLine(index: IndexSummary, noteCount: number): string | null {
  if (noteCount === 0) return null;
  const opening =
    "Every chat starts with the one-line summaries below (the list in MEMORY.md), and opens a note's full text only when its summary looks relevant to what you're asking.";
  const limit = `Chats load the first ${index.max_lines} lines or ${Math.round(index.max_bytes / 1024)}KB of that list, whichever is less`;
  const skipped = index.line_count - index.loaded_line_count;
  return skipped > 0
    ? `${opening} ${limit}; yours is ${countLabel(index.line_count, "line", "lines")} long, so the last ${countLabel(skipped, "line isn't", "lines aren't")} loaded.`
    : `${opening} ${limit}; yours is ${countLabel(index.line_count, "line", "lines")} long.`;
}

type IndexStatusKind = "listed" | "not-listed" | "past-limit";

function indexStatusKind(note: Note): IndexStatusKind {
  if (note.index_entry === null) return "not-listed";
  return note.index_entry.is_loaded ? "listed" : "past-limit";
}

/** Why chats won't see a note at the start, or null when they will. */
export function indexWarning(note: Note): string | null {
  switch (indexStatusKind(note)) {
    case "listed":
      return null;
    case "not-listed":
      return "Not in the list chats start with, so chats are unlikely to use it. Edit it to add it back.";
    case "past-limit":
      return "Past what chats load from that list, so they don't see it";
  }
}

/** The backups fact shown at the top: deleted notes outlive their delete there, so it says so. */
export function backupsChip(backups: BackupRetention): { readonly text: string; readonly isWarning: boolean } {
  const kept = keptFor(backups);
  return kept === null
    ? { text: "Not backed up", isWarning: false }
    : { text: `Backups keep deleted notes up to ${kept}`, isWarning: true };
}

/** One line naming what in the list chats load is wrong, or null when nothing is. */
export function attentionLine(notes: readonly Note[], missingFiles: readonly string[]): string | null {
  const countByKind: Record<IndexStatusKind, number> = { listed: 0, "not-listed": 0, "past-limit": 0 };
  for (const note of notes) countByKind[indexStatusKind(note)] += 1;
  const notListed = countByKind["not-listed"];
  const pastLimit = countByKind["past-limit"];
  const parts = [
    notListed === 0
      ? null
      : `${countLabel(notListed, "note isn't", "notes aren't")} in the list chats start with, so chats are unlikely to use ${notListed === 1 ? "it" : "them"}. Editing a note adds it back.`,
    pastLimit === 0
      ? null
      : `${countLabel(pastLimit, "note is", "notes are")} past what chats load from that list, so they don't see ${pastLimit === 1 ? "it" : "them"}.`,
    missingFiles.length === 0
      ? null
      : `The list chats start with still names ${countLabel(missingFiles.length, "note that no longer exists", "notes that no longer exist")}.`,
  ].filter((part): part is string => part !== null);
  return parts.length === 0 ? null : parts.join(" ");
}

export const HARNESS_LABEL: Readonly<Record<MemoryHarness, string>> = { CLAUDE: "Claude", PI_CODING: "pi" };
export const MEMORY_HARNESSES: readonly MemoryHarness[] = ["CLAUDE", "PI_CODING"];

/** The kinds of chat memory is on for. Unreadable settings count as off, as chats read them. */
export function harnessesUsingMemory(controls: MemoryControls | null): readonly MemoryHarness[] {
  if (controls === null || controls.is_paused) return [];
  return MEMORY_HARNESSES.filter((harness) => !controls.disabled_harnesses.includes(harness));
}

/** The first fact at the top: which chats use the notes right now. */
export function sharingChip(controls: MemoryControls | null): { readonly text: string; readonly isOff: boolean } {
  if (controls === null) return { text: "Memory off: settings unreadable", isOff: true };
  if (controls.is_paused) return { text: "Memory paused", isOff: true };
  const using = harnessesUsingMemory(controls);
  if (using.length === 0) return { text: "Memory off for every chat", isOff: true };
  if (using.length === MEMORY_HARNESSES.length) return { text: "Shared with your Claude and pi chats", isOff: false };
  return {
    text: `Used by your ${using.map((harness) => HARNESS_LABEL[harness]).join(" and ")} chats only`,
    isOff: false,
  };
}

/** Who reads the notes, for "Where your notes go". */
export function readersLine(controls: MemoryControls | null): string {
  const using = harnessesUsingMemory(controls);
  if (using.length === 0) return "No chats while memory is off. The notes are kept, and used again when it's back on.";
  return `Every ${using.map((harness) => HARNESS_LABEL[harness]).join(" and ")} chat in this workspace, including new ones.`;
}
