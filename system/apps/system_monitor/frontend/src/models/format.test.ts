import { describe, expect, it } from "vitest";

import { GB, MB, backupsOf, statsOf, usageOf } from "../testing/records";
import {
  ago,
  backupLines,
  backupSchedule,
  cpuRows,
  freeingAdvice,
  memoryNote,
  sharePct,
  shownRows,
  size,
  usageTag,
} from "./format";

describe("figures", () => {
  it("says a size in GB from 1 GB, with a decimal below 10, and in MB below", () => {
    expect(size(12.4 * GB)).toBe("12 GB");
    expect(size(1.55 * GB)).toBe("1.6 GB");
    expect(size(300 * MB)).toBe("300 MB");
  });

  it("says a CPU share with a decimal below 10%, and none for no share", () => {
    expect(sharePct(0.37)).toBe("37%");
    expect(sharePct(0.034)).toBe("3.4%");
    expect(sharePct(null)).toBeNull();
    expect(sharePct(undefined)).toBeNull();
  });

  it("says how long ago in the coarsest unit that fits", () => {
    expect(ago(30)).toBe("just now");
    expect(ago(600)).toBe("10 min ago");
    expect(ago(3 * 3600)).toBe("3 h ago");
    expect(ago(2 * 86400)).toBe("2 d ago");
  });
});

describe("the breakdown", () => {
  it("tags a row with its count, its hint, or the scratch space's limit", () => {
    const [chats, checkers, workspace, , scratch] = usageOf();
    expect(usageTag(chats)).toBe("2 running");
    expect(usageTag(checkers)).toBe("none running");
    expect(usageTag(workspace)).toBe("");
    expect(usageTag({ ...workspace, hint: "the desktop and services" })).toBe("the desktop and services");
    expect(usageTag(scratch)).toBe("of 1.0 GB limit");
  });

  it("leaves out Code checkers and Your apps while nothing runs under them", () => {
    expect(shownRows(usageOf()).map((row) => row.id)).toEqual(["chats", "workspace", "scratch"]);
  });

  it("sorts the CPU view's rows and their items by share, and keeps the order without a reading", () => {
    const sorted = cpuRows(statsOf());
    expect(sorted.map((row) => row.id)).toEqual(["workspace", "chats"]);
    expect(sorted[1].items.map((item) => item.name)).toEqual(["Report", "Inbox"]);

    const unread = cpuRows(statsOf({ cpu: { usage: 0, cores: 4, processes: 1, sampled_at: null } }));
    expect(unread.map((row) => row.id)).toEqual(["chats", "workspace"]);
  });
});

describe("the memory note", () => {
  it("says nothing while memory is fine and nothing was stopped", () => {
    expect(memoryNote(statsOf())).toBeNull();
  });

  it("says what was stopped even when memory is fine", () => {
    const note = memoryNote(
      statsOf({ stops: [{ at: "2026-11-12T00:00:00Z", what: "a chat's agent", command: null }] }),
    );
    expect(note).toEqual({
      head: "Memory is 50% full.",
      rest: "The workspace stopped 1 program in the last 24 hours.",
    });
  });

  it("warns from 80% and names the largest memory the user can free", () => {
    const note = memoryNote(statsOf({ memory: { total: 8 * GB, available: GB, used: 7 * GB } }));
    expect(note?.head).toBe("Memory is 88% full.");
    expect(note?.rest).toBe(
      "Above 90%, the workspace stops programs to free memory. Chats are using the most memory. Stopping chats you " +
        "aren't using frees memory.",
    );
  });

  it("gives no advice when what the user can free is small", () => {
    const usage = usageOf().map((row) => ({ ...row, bytes: 10 * MB }));
    expect(freeingAdvice(usage)).toBe("");
  });
});

describe("the backups", () => {
  const now = Date.parse("2026-11-12T12:00:00Z");

  it("says how long ago the last backup completed, or that it failed", () => {
    const up = backupsOf({ last_success_at: "2026-11-12T11:30:00Z" });
    expect(backupLines(up, now)).toEqual(["live", "Backed up 30 min ago.", "Up to date."]);
    const failing = backupsOf({ state: "failing", last_failure_at: "2026-11-12T10:00:00Z", last_success_at: null });
    expect(backupLines(failing, now)).toEqual(["bad", "The last backup failed 2 h ago.", "None has completed yet."]);
    expect(backupLines(backupsOf({ state: "not_configured" }), now)[0]).toBe("none");
  });

  it("says how often backups run and how many failed today", () => {
    expect(backupSchedule(backupsOf())).toBe("The whole workspace is backed up every 6 hours, encrypted.");
    expect(backupSchedule(backupsOf({ interval_seconds: 900, failures_last_day: 2 }))).toBe(
      "The whole workspace is backed up every 15 minutes, encrypted. 2 backups failed in the last 24 hours.",
    );
  });
});
