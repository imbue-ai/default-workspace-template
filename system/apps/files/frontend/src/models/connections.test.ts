import { describe, expect, it } from "vitest";

import { type Connections, type Share, describeShare, deviceNameOf, isLiveShared } from "./connections";

const MAC = "Kims-Mac";

function share(overrides: Partial<Share>): Share {
  return {
    device: "host-mac",
    path: "/Users/kim/notes",
    access: "write",
    kind: "folder",
    is_reachable: true,
    synced_copy: null,
    sync_state: null,
    ...overrides,
  };
}

describe("describeShare", () => {
  it("says how a share reaches the workspace and how it stands, never its name", () => {
    const copy = "/home/user/synced_folders/host-mac/Users/kim/notes/";
    expect(describeShare(share({ synced_copy: copy, sync_state: "current" }), MAC)).toBe(
      "Synced with Kims-Mac. Up to date.",
    );
    expect(describeShare(share({ access: "read", synced_copy: copy, sync_state: "behind" }), MAC)).toBe(
      "Synced from Kims-Mac. Read-only. Hasn't caught up yet.",
    );
    expect(describeShare(share({ synced_copy: copy, sync_state: "unknown", is_reachable: false }), MAC)).toBe(
      "Synced with Kims-Mac. Can't check: not answering right now.",
    );
    expect(describeShare(share({}), MAC)).toBe("On Kims-Mac.");
    expect(describeShare(share({ access: "read", is_reachable: false }), MAC)).toBe(
      "On Kims-Mac. Read-only. Not answering right now.",
    );
    expect(describeShare(share({}), MAC)).not.toContain("notes");
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
