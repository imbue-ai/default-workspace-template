/**
 * The words the page shows for a note: who wrote it and who read it, dates, counts, the note's body split
 * into paragraphs and labeled lines (``**Why:** ...``), and what the backups still hold after a delete. Pure, so
 * each is tested on its own; the body is returned as plain text pieces, never HTML, since an agent wrote it.
 */

import type { AuthorKind, BackupRetention, Note, NoteAttribution } from "../models/notes";

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

/** The standing line in "Who can see these notes": deleted notes stay in the backups. */
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
