import { describe, expect, it } from "vitest";
import type { KeybindingAppView } from "../model/api";
import { chordsLabel, conflictingAction } from "./KeyboardSettingsSection";

const APP: KeybindingAppView = {
  app: "files",
  display_name: "Files",
  actions: [
    {
      id: "rename",
      label: "Rename the selected entry",
      default_chords: ["f2"],
      chords: ["f2"],
      source: "default",
    },
    {
      id: "delete",
      label: "Delete the selection",
      default_chords: ["delete"],
      chords: ["delete", "ctrl+backspace"],
      source: "workspace",
    },
  ],
};

describe("chordsLabel", () => {
  it("joins the chords and names an unbound action", () => {
    expect(chordsLabel(["ctrl+a", "meta+a"])).toBe("ctrl+a, meta+a");
    expect(chordsLabel([])).toBe("not bound");
  });
});

describe("conflictingAction", () => {
  it("finds another action of the same app holding the chord, and never the action itself", () => {
    expect(conflictingAction(APP, "rename", "ctrl+backspace")?.id).toBe("delete");
    expect(conflictingAction(APP, "delete", "ctrl+backspace")).toBeNull();
    expect(conflictingAction(APP, "rename", "ctrl+n")).toBeNull();
  });
});
