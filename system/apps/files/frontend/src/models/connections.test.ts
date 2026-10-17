import { describe, expect, it } from "vitest";

import {
  type Connections,
  type FolderSync,
  type Share,
  describeShare,
  deviceNameOf,
  isLiveShared,
  syncNoticeOf,
} from "./connections";
import { formatModified } from "./format";

const MAC = "Kims-Mac";
const COPY = "/home/user/synced_folders/host-mac/Users/kim/notes/";

function share(overrides: Partial<Share>): Share {
  return {
    device: "host-mac",
    path: "/Users/kim/notes",
    access: "write",
    kind: "folder",
    is_reachable: true,
    synced_copy: null,
    sync: null,
    ...overrides,
  };
}

function sync(overrides: Partial<FolderSync>): FolderSync {
  return {
    state: "SYNCED",
    message: null,
    bytes_done: null,
    bytes_total: null,
    has_finished_first_copy: true,
    last_synced_at: null,
    ...overrides,
  };
}

function synced(overrides: Partial<FolderSync>, shareOverrides: Partial<Share> = {}): Share {
  return share({ synced_copy: COPY, sync: sync(overrides), ...shareOverrides });
}

describe("describeShare", () => {
  it("says how a share reaches the workspace and how it stands, never its name", () => {
    expect(describeShare(synced({}), MAC)).toBe("Synced with Kims-Mac. Up to date.");
    expect(describeShare(share({ synced_copy: COPY, is_reachable: false }), MAC)).toBe(
      "Synced with Kims-Mac. Can't check: not answering right now.",
    );
    expect(describeShare(share({}), MAC)).toBe("On Kims-Mac.");
    expect(describeShare(share({ access: "read", is_reachable: false }), MAC)).toBe(
      "On Kims-Mac. Read-only. Not answering right now.",
    );
    expect(describeShare(share({}), MAC)).not.toContain("notes");
  });

  it("says how a sync stands as Imbue Studio reports it", () => {
    const when = "2026-10-17T09:30:00Z";
    const day = formatModified(Date.parse(when)).replace(/^(Today|Yesterday)/, (word) => word.toLowerCase());
    expect(describeShare(synced({ last_synced_at: when }), MAC)).toBe(
      `Synced with Kims-Mac. Up to date, last synced ${day}.`,
    );
    expect(
      describeShare(
        synced({ state: "SYNCING", has_finished_first_copy: false, bytes_done: 10_000_000, bytes_total: 40_000_000 }),
        MAC,
      ),
    ).toBe("Synced with Kims-Mac. Copying the first time, 10 MB of 40 MB.");
    // Synced before its first pass finished is still copying: Imbue Studio says so as soon as the sync starts.
    expect(describeShare(synced({ has_finished_first_copy: false }), MAC)).toBe(
      "Synced with Kims-Mac. Copying the first time.",
    );
    expect(describeShare(synced({ state: "SYNCING" }), MAC)).toBe("Synced with Kims-Mac. Syncing.");
    expect(
      describeShare(synced({ state: "FAILED", message: "Unison exited with code 3" }, { access: "read" }), MAC),
    ).toBe("Synced from Kims-Mac. Read-only. Sync stopped: Unison exited with code 3");
    expect(describeShare(synced({ state: "STOPPED" }), MAC)).toBe("Synced with Kims-Mac. Not syncing right now.");
  });
});

describe("syncNoticeOf", () => {
  const listed = (only: Share): Connections => ({
    devices: [{ id: "host-mac", name: "Kims-Mac.local" }],
    shares: [only],
  });

  it("says inside a copy that its sync stopped, and why, wherever in it you are", () => {
    const notice = syncNoticeOf(listed(synced({ state: "FAILED", message: "Disk full." })), `${COPY}drafts/`);
    expect(notice).toEqual({
      tone: "warning",
      title: "Sync with Kims-Mac stopped",
      detail: "Disk full. Retry it in Imbue Studio on your computer.",
      isOnlyWhenEmpty: false,
    });
  });

  it("stands in for an empty copy that is still being copied the first time", () => {
    const copying = synced({
      state: "SYNCING",
      has_finished_first_copy: false,
      bytes_done: 2_000,
      bytes_total: 5_000,
    });
    expect(syncNoticeOf(listed(copying), COPY)).toEqual({
      tone: "progress",
      title: "Copying from Kims-Mac",
      detail: "2.0 KB of 5.0 KB so far.",
      isOnlyWhenEmpty: true,
    });
  });

  it("has nothing to say about a copy that is up to date, or one whose computer didn't say", () => {
    expect(syncNoticeOf(listed(synced({})), COPY)).toBeNull();
    expect(syncNoticeOf(listed(share({ synced_copy: COPY })), COPY)).toBeNull();
  });
});

describe("isLiveShared", () => {
  it("is true only under the live view of a computer, not in a synced copy", () => {
    expect(isLiveShared("/__remote/host-mac/Users/kim/notes/")).toBe(true);
    expect(isLiveShared("/home/user/synced_folders/host-mac/Users/kim/notes/")).toBe(false);
  });
});

describe("deviceNameOf", () => {
  it("names a computer as people say it, without the .local macOS adds", () => {
    const connections: Connections = { devices: [{ id: "host-mac", name: "Kims-Mac.local" }], shares: [] };
    expect(deviceNameOf(connections, "host-mac")).toBe("Kims-Mac");
    expect(deviceNameOf(connections, "host-gone")).toBe("your computer");
  });
});
