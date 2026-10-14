import { describe, expect, it } from "vitest";
import type { BackupRetention, Note, NoteAuthor } from "../models/notes";
import {
  attributionLine,
  backupsDetail,
  backupsLine,
  deleteBackupsWarning,
  deletedStatus,
  formatDate,
  writerLine,
} from "./format";

const BACKED_UP: BackupRetention = {
  is_backed_up: true,
  longest_kept: "24 months",
  schedule: ["hourly for 24 hours", "daily for 30 days", "weekly for 12 weeks", "monthly for 24 months"],
  settings_path: "data/system/backup.toml",
};
const NOT_BACKED_UP: BackupRetention = { ...BACKED_UP, is_backed_up: false };
const KEEPS_NOTHING: BackupRetention = { ...BACKED_UP, longest_kept: null, schedule: [] };

describe("what the page says the backups keep after a delete", () => {
  it("names how long a backed-up workspace keeps a deleted note, everywhere it is mentioned", () => {
    expect(backupsLine(BACKED_UP)).toBe(
      "Your workspace's backups also hold these notes, including ones you delete, for up to 24 months.",
    );
    expect(deleteBackupsWarning(BACKED_UP)).toBe(
      "A copy stays in your workspace's backups until they expire, up to 24 months from now.",
    );
    expect(deletedStatus(BACKED_UP)).toBe("Deleted. A copy stays in your backups for up to 24 months.");
    expect(backupsDetail(BACKED_UP)).toContain(
      "kept hourly for 24 hours, daily for 30 days, weekly for 12 weeks, monthly for 24 months (set in data/system/backup.toml)",
    );
  });

  it("says a delete is final when nothing outside the workspace keeps a copy", () => {
    for (const backups of [NOT_BACKED_UP, KEEPS_NOTHING]) {
      expect(backupsLine(backups)).toBe("This workspace isn't backed up, so a note you delete is gone for good.");
      expect(deleteBackupsWarning(backups)).toBe("This workspace isn't backed up, so no other copy is kept.");
      expect(deletedStatus(backups)).toBe("Deleted.");
    }
    expect(backupsDetail(NOT_BACKED_UP)).toBe("No backups are set up for this workspace.");
    expect(backupsDetail(KEEPS_NOTHING)).toBe("Backups are set up, but data/system/backup.toml keeps no snapshots.");
  });
});

function savedBy(source: string | null, chatTitle: string | null = null): Note {
  return {
    file_name: "a.md",
    name: "a",
    description: "A",
    note_type: "USER",
    source,
    body: "",
    raw_text: "",
    modified_at: "2026-10-01T10:00:00Z",
    version: "1-1",
    attribution:
      chatTitle === null ? null : { authors: [{ kind: "CHAT", chat_title: chatTitle, at: null }], reader_count: 0 },
  };
}

describe("who saved a note", () => {
  it("names the Claude chat from its transcript, and other harnesses by name", () => {
    expect(writerLine(savedBy("claude", "Plan the launch"))).toBe(
      'Written by "Plan the launch" · not opened by another chat yet',
    );
    expect(writerLine(savedBy(null))).toBe("Who wrote this isn't recorded");
    expect(writerLine(savedBy("pi-coding"))).toBe("Saved by a pi chat");
    expect(writerLine(savedBy("codex"))).toBe("Saved by a Codex chat");
    expect(writerLine(savedBy("something-new"))).toBe("Saved by a something-new chat");
    expect(writerLine(savedBy("pi-coding", "pi-test"))).toBe('Written by "pi-test" · not opened by another chat yet');
  });
});

describe("who wrote a note", () => {
  const author = (kind: NoteAuthor["kind"], chatTitle: string | null = null): NoteAuthor => ({
    kind,
    chat_title: chatTitle,
    at: null,
  });

  it("names a live chat, and says honestly what is known about any other writer", () => {
    expect(attributionLine({ authors: [author("CHAT", "Plan the launch")], reader_count: 2 })).toBe(
      'Written by "Plan the launch" · read by 2 chats',
    );
    expect(attributionLine({ authors: [author("NOT_A_CHAT")], reader_count: 0 })).toBe(
      "Written by a chat that has since been deleted, or a background task · not opened by another chat yet",
    );
    expect(attributionLine({ authors: [author("UNKNOWN"), author("CHAT", "Other")], reader_count: 1 })).toBe(
      "Written by a chat whose name couldn't be read and 1 other chat · read by 1 chat",
    );
    expect(attributionLine(null)).toBe("Who wrote this isn't recorded");
  });
});

describe("dates", () => {
  it("shows the year only for a date before this year", () => {
    const now = new Date("2026-10-01T12:00:00Z");

    expect(formatDate("2026-03-04T12:00:00Z", now)).not.toMatch(/2026/);
    expect(formatDate("2025-03-04T12:00:00Z", now)).toMatch(/2025/);
  });
});
