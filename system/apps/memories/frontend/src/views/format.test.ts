import { describe, expect, it } from "vitest";
import type { BackupRetention } from "../models/notes";
import { backupsDetail, backupsLine, deleteBackupsWarning, deletedStatus } from "./format";

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
