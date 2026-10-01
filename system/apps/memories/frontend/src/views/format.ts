/**
 * The words the page shows for a note: who wrote it and who read it, dates, counts, and the note's body split
 * into paragraphs and labeled lines (``**Why:** ...``). Pure, so each is tested on its own; the body is returned as
 * plain text pieces, never HTML, since an agent wrote it.
 */

import type { NoteAttribution } from "../models/notes";

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
      return labeled === null ? { label: null, text: plainText(line) } : { label: labeled[1], text: plainText(labeled[2]) };
    });
}

export function countLabel(count: number, singular: string, plural: string): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** "Written by "Plan the launch" · read by 2 chats", in whatever part the transcripts can support. */
export function attributionLine(attribution: NoteAttribution | null): string {
  if (attribution === null) return "Who wrote this isn't recorded";
  const latest = attribution.authors[0];
  const writer =
    latest === undefined
      ? "Who wrote this isn't recorded"
      : latest.chat_title === null
        ? "Written by a chat that has since been deleted"
        : `Written by "${latest.chat_title}"`;
  const others = attribution.authors.length > 1 ? ` and ${countLabel(attribution.authors.length - 1, "other chat", "other chats")}` : "";
  const readers =
    attribution.reader_count === 0 ? "not read since" : `read by ${countLabel(attribution.reader_count, "chat", "chats")}`;
  return `${writer}${others} · ${readers}`;
}
