import { describe, expect, it } from "vitest";

import { memoryStorage } from "../testing/records";
import { DEFAULT_PREFERENCES, loadPreferences, savePreferences } from "./preferences";

describe("preferences", () => {
  it("start at icons by name with system files hidden", () => {
    expect(loadPreferences(memoryStorage())).toEqual(DEFAULT_PREFERENCES);
  });

  it("round-trip through storage", () => {
    const storage = memoryStorage();
    const chosen = {
      viewMode: "columns" as const,
      sort: { key: "size" as const, descending: true },
      showHidden: true,
      sidebarWidth: 240,
    };
    savePreferences(storage, chosen);
    expect(loadPreferences(storage)).toEqual(chosen);
  });

  it("keep the File Viewer's system-files choice and ignore values they do not know", () => {
    const storage = memoryStorage();
    storage.setItem("minds-files-show-hidden", "true");
    storage.setItem("files-view-mode", "gallery");
    storage.setItem("files-sort", "colour:desc");
    storage.setItem("files-sidebar-width", "wide");
    expect(loadPreferences(storage)).toEqual({ ...DEFAULT_PREFERENCES, showHidden: true });
  });

  it("keep a stored sidebar width between its bounds", () => {
    const storage = memoryStorage();
    storage.setItem("files-sidebar-width", "9000");
    expect(loadPreferences(storage).sidebarWidth).toBe(360);
    storage.setItem("files-sidebar-width", "12");
    expect(loadPreferences(storage).sidebarWidth).toBe(140);
  });
});
