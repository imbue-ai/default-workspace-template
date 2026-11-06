import { describe, expect, it } from "vitest";

import {
  type Connections,
  type FolderSync,
  type Share,
  describeShare,
  deviceNameOf,
  isLiveShared,
  shareBadgeOf,
  isAnySyncMoving,
  syncNoticeOf,
} from "./connections";

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
    ...overrides,
  };
}

function synced(overrides: Partial<FolderSync>, shareOverrides: Partial<Share> = {}): Share {
  return share({ synced_copy: COPY, sync: sync(overrides), ...shareOverrides });
}

describe("describeShare", () => {
  const tip = (text: string, detail: string | null = null) => ({ text, detail });

  it("names the computer and how the share stands, never the share's name or which way it syncs", () => {
    expect(describeShare(synced({}), MAC)).toEqual(tip("Up to date with Kims-Mac"));
    // A one-way sync reads the same: its read-only badge says the rest.
    expect(describeShare(synced({}, { access: "read" }), MAC)).toEqual(tip("Up to date with Kims-Mac"));
    expect(describeShare(share({ synced_copy: COPY, is_reachable: false }), MAC)).toEqual(
      tip("Kims-Mac isn't answering"),
    );
    expect(describeShare(share({}), MAC)).toEqual(tip("On Kims-Mac"));
    expect(describeShare(share({ access: "read", is_reachable: false }), MAC)).toEqual(
      tip("Kims-Mac isn't answering"),
    );
    expect(JSON.stringify(describeShare(share({}), MAC))).not.toContain("notes");
  });

  it("puts the number or the reason behind a sync's state on a quieter second line", () => {
    expect(describeShare(synced({ state: "SYNCING", bytes_done: 10_000_000, bytes_total: 40_000_000 }), MAC)).toEqual(
      tip("Syncing with Kims-Mac", "25% done"),
    );
    // A sync on its way up is syncing, with no number until a transfer is counted.
    expect(describeShare(synced({ state: "STARTING" }), MAC)).toEqual(tip("Syncing with Kims-Mac"));
    // A pass that has moved every byte is still syncing until it settles, so it never claims 100%.
    expect(describeShare(synced({ state: "SYNCING", bytes_done: 40, bytes_total: 40 }), MAC).detail).toBe("99% done");
    expect(describeShare(synced({ state: "FAILED", message: "Unison exited with code 3." }), MAC)).toEqual(
      tip("Sync with Kims-Mac stopped", "Unison exited with code 3"),
    );
    expect(describeShare(synced({ state: "FAILED" }), MAC)).toEqual(tip("Sync with Kims-Mac stopped"));
    // Studio has it switched on, yet nothing runs: said plainly, as it mostly passes in a moment.
    expect(describeShare(synced({ state: "STOPPED" }), MAC)).toEqual(tip("Not syncing with Kims-Mac right now"));
    expect(describeShare(synced({ state: "UNKNOWN" }), MAC)).toEqual(tip("Not syncing with Kims-Mac right now"));
  });
});

describe("shareBadgeOf", () => {
  it("chains a live share, and turns a synced one's arrows only while its copy is moving", () => {
    expect(shareBadgeOf(share({}))).toBe("linked");
    expect(shareBadgeOf(synced({}))).toBe("synced");
    expect(shareBadgeOf(synced({ state: "SYNCING" }))).toBe("syncing");
    expect(shareBadgeOf(synced({ state: "STARTING" }))).toBe("syncing");
    expect(shareBadgeOf(synced({ state: "FAILED" }))).toBe("synced");
    expect(shareBadgeOf(share({ synced_copy: COPY }))).toBe("synced");
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

  it("stands in for an empty copy while it syncs, and only while it is empty", () => {
    const moving = synced({ state: "SYNCING", bytes_done: 2_000, bytes_total: 5_000 });
    expect(syncNoticeOf(listed(moving), COPY)).toEqual({
      tone: "progress",
      title: "Syncing from Kims-Mac",
      detail: "40% done",
      isOnlyWhenEmpty: true,
    });
    expect(syncNoticeOf(listed(synced({ state: "STARTING" })), COPY)?.detail).toBe("Files appear here as they arrive");
  });

  it("has nothing to say about a copy that is up to date, not syncing for now, or one whose computer didn't say", () => {
    expect(syncNoticeOf(listed(synced({})), COPY)).toBeNull();
    expect(syncNoticeOf(listed(synced({ state: "STOPPED" })), COPY)).toBeNull();
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

describe("isAnySyncMoving", () => {
  const listed = (...shares: Share[]): Connections => ({ devices: [], shares });

  it("is true while a copy is carrying something across, and not for one that is settled, stopped or silent", () => {
    expect(isAnySyncMoving(listed(synced({}), synced({ state: "SYNCING" })))).toBe(true);
    expect(isAnySyncMoving(listed(synced({ state: "STARTING" })))).toBe(true);
    expect(isAnySyncMoving(listed(synced({}), synced({ state: "FAILED" }), share({ synced_copy: COPY })))).toBe(false);
    expect(isAnySyncMoving(null)).toBe(false);
  });
});
