import { describe, expect, it } from "vitest";
import type { BackupRetention, IndexSummary, Note, NoteAuthor } from "../models/notes";
import {
  attentionLine,
  attributionLine,
  backupsChip,
  backupsDetail,
  backupsLine,
  deleteBackupsWarning,
  deletedStatus,
  formatDate,
  readersLine,
  sharingChip,
  indexWarning,
  indexSummaryLine,
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
    index_entry: null,
  };
}

describe("who saved a note", () => {
  it("names the Claude chat from its transcript, and other harnesses by name", () => {
    expect(writerLine(savedBy("claude", "Plan the launch"))).toBe('Written by "Plan the launch" · not read since');
    expect(writerLine(savedBy(null))).toBe("Who wrote this isn't recorded");
    expect(writerLine(savedBy("pi-coding"))).toBe("Saved by a pi chat");
    expect(writerLine(savedBy("codex"))).toBe("Saved by a Codex chat");
    expect(writerLine(savedBy("something-new"))).toBe("Saved by a something-new chat");
    expect(writerLine(savedBy("pi-coding", "pi-test"))).toBe('Written by "pi-test" · not read since');
  });
});

const INDEX: IndexSummary = {
  line_count: 3,
  loaded_line_count: 3,
  max_lines: 200,
  max_bytes: 25600,
  missing_files: [],
};

describe("what chats load of the notes", () => {
  it("says every chat starts with the summaries, the load limit, and how much of the list is cut", () => {
    expect(indexSummaryLine(INDEX, 3)).toBe(
      "Every chat starts with the one-line summaries below (the list in MEMORY.md), and opens a note's full text only when its summary looks relevant to what you're asking. Chats load the first 200 lines or 25KB of that list, whichever is less; yours is 3 lines long.",
    );
    expect(indexSummaryLine({ ...INDEX, line_count: 203, loaded_line_count: 200 }, 203)).toContain(
      "yours is 203 lines long, so the last 3 lines aren't loaded.",
    );
    expect(indexSummaryLine({ ...INDEX, line_count: 100, loaded_line_count: 99 }, 100)).toContain(
      "so the last 1 line isn't loaded.",
    );
    expect(indexSummaryLine(INDEX, 0)).toBeNull();
  });

  it("warns only about a note chats won't see at the start, and says how to fix a missing one", () => {
    const listed = (isLoaded: boolean): Note => ({
      ...savedBy("claude"),
      index_entry: { is_loaded: isLoaded },
    });

    expect(indexWarning(listed(true))).toBeNull();
    expect(indexWarning(listed(false))).toBe("Past what chats load from that list, so they don't see it");
    expect(indexWarning(savedBy("claude"))).toBe(
      "Not in the list chats start with, so chats are unlikely to use it. Edit it to add it back.",
    );
  });
});

describe("the short facts and warnings at the top", () => {
  it("names how long backups keep deleted notes, or that there are none", () => {
    expect(backupsChip(BACKED_UP)).toEqual({ text: "Backups keep deleted notes up to 24 months", isWarning: true });
    expect(backupsChip(NOT_BACKED_UP)).toEqual({ text: "Not backed up", isWarning: false });
    expect(backupsChip(KEEPS_NOTHING)).toEqual({ text: "Not backed up", isWarning: false });
  });

  it("names what is wrong with the list chats load, and says nothing when it's all fine", () => {
    const seen = { ...savedBy("claude"), index_entry: { is_loaded: true } };
    const pastLimit = { ...seen, index_entry: { is_loaded: false } };
    const notListed = savedBy("claude");

    expect(attentionLine([seen], [])).toBeNull();
    expect(attentionLine([seen, notListed], [])).toBe(
      "1 note isn't in the list chats start with, so chats are unlikely to use it. Editing a note adds it back.",
    );
    expect(attentionLine([notListed, notListed, pastLimit], ["gone.md"])).toBe(
      "2 notes aren't in the list chats start with, so chats are unlikely to use them. Editing a note adds it back. 1 note is past what chats load from that list, so they don't see it. The list chats start with still names 1 note that no longer exists.",
    );
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
      "Written by a chat that has since been deleted, or a background task · not read since",
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

describe("which chats use the notes", () => {
  it("names the chats memory is on for, or why it is off", () => {
    expect(sharingChip({ is_paused: false, disabled_harnesses: [] })).toEqual({
      text: "Shared with your Claude and pi chats",
      isOff: false,
    });
    expect(sharingChip({ is_paused: false, disabled_harnesses: ["CLAUDE"] }).text).toBe("Used by your pi chats only");
    expect(sharingChip({ is_paused: false, disabled_harnesses: ["CLAUDE", "PI_CODING"] })).toEqual({
      text: "Memory off for every chat",
      isOff: true,
    });
    expect(sharingChip({ is_paused: true, disabled_harnesses: [] }).text).toBe("Memory paused");
    expect(sharingChip(null).text).toBe("Memory off: settings unreadable");
    expect(readersLine({ is_paused: true, disabled_harnesses: [] })).toBe(
      "No chats while memory is off. The notes are kept, and used again when it's back on.",
    );
  });
});
