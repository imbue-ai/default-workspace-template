import { describe, expect, it } from "vitest";

import { isOwnSaveId, mintSaveId, mintTabId, panelsWithUnlistedAddresses } from "./Layouts";

describe("tab ids", () => {
  it("mints ids in the fixed shape, never twice", () => {
    const first = mintTabId();
    expect(first).toMatch(/^tab-[0-9a-f]{16}$/);
    expect(mintTabId()).not.toBe(first);
  });
});

describe("save ids", () => {
  it("mints ids in the fixed shape and remembers the ones this window minted", () => {
    const saveId = mintSaveId();
    expect(saveId).toMatch(/^save-[0-9a-f]{16}$/);
    expect(isOwnSaveId(saveId)).toBe(true);
    expect(isOwnSaveId("save-0123456789abcdef")).toBe(false);
    expect(mintSaveId()).not.toBe(saveId);
  });

  it("forgets the oldest ids once enough later ones were minted", () => {
    const oldest = mintSaveId();
    for (let index = 0; index < 64; index += 1) mintSaveId();
    expect(isOwnSaveId(oldest)).toBe(false);
  });
});

describe("panelsWithUnlistedAddresses", () => {
  it("names the panels whose address no app lists any more", () => {
    const tabs = {
      p1: { address: "app:files", tab_id: "tab-0000000000000001", last_focused_ms: 0 },
      p2: { address: "app:terminal?instance=terminal-9", tab_id: "tab-0000000000000002", last_focused_ms: 0 },
    };
    expect(panelsWithUnlistedAddresses(tabs, (address) => address === "app:files")).toEqual(["p2"]);
  });
});
