// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { KeybindingAppView, KeybindingsCatalog, KeybindingsDeliveryOutcome } from "../model/api";
import { isShortcutRecording } from "../model/shortcutRecording";
import { settle } from "../testing/fakeShell";
import {
  KeyboardSettingsSection,
  actionsSharingShortcuts,
  clashesOf,
  conflictingAction,
  reservedChordNotice,
  unsentEntryLabel,
} from "./KeyboardSettingsSection";

const APP: KeybindingAppView = {
  app: "files",
  display_name: "Files",
  actions: [
    {
      id: "rename",
      label: "Rename the selected entry",
      default_chords: ["f2"],
      chords: ["f2"],
      reset_chords: ["f2"],
      source: "default",
    },
    {
      id: "delete",
      label: "Delete the selection",
      default_chords: ["delete"],
      chords: ["delete", "ctrl+backspace"],
      reset_chords: ["delete"],
      source: "workspace",
    },
    {
      id: "new-folder",
      label: "New folder",
      default_chords: ["ctrl+shift+n"],
      chords: [],
      reset_chords: [],
      source: "account",
    },
  ],
};

/** Rename's F2 moved to Delete in this workspace: resetting Rename would bring F2 back while Delete has it. */
const APP_AFTER_REPLACE: KeybindingAppView = {
  ...APP,
  actions: [
    { ...APP.actions[0], chords: [], source: "workspace" },
    { ...APP.actions[1], chords: ["delete", "f2"] },
    APP.actions[2],
  ],
};

/** F2 on both Rename and Delete, as layers this screen does not edit could leave it. */
const APP_SHARING_F2: KeybindingAppView = {
  ...APP,
  actions: [APP.actions[0], { ...APP.actions[1], chords: ["delete", "f2"] }, APP.actions[2]],
};

function catalogOf(overrides: Partial<KeybindingsCatalog> = {}): KeybindingsCatalog {
  return {
    apps: [APP],
    is_owner: true,
    can_save_everywhere: true,
    has_pending_permission: false,
    unsent: [],
    ...overrides,
  };
}

describe("conflictingAction", () => {
  it("finds another action of the same app holding the chord, and never the action itself", () => {
    expect(conflictingAction(APP, "rename", "ctrl+backspace")?.id).toBe("delete");
    expect(conflictingAction(APP, "delete", "ctrl+backspace")).toBeNull();
    expect(conflictingAction(APP, "rename", "ctrl+n")).toBeNull();
  });

  it("reads an action's chords from the edits made so far, ahead of what it was loaded with", () => {
    const afterReplace = new Map([["delete", ["delete"]]]);
    expect(conflictingAction(APP, "rename", "ctrl+backspace", afterReplace)).toBeNull();
  });
});

describe("clashesOf", () => {
  it("lists each of the chords another action uses, with the action that has it", () => {
    const entries = clashesOf(APP_AFTER_REPLACE, "rename", ["f2", "ctrl+x"]);
    expect(entries.map((entry) => [entry.chord, entry.otherAction.id])).toEqual([["f2", "delete"]]);
    expect(clashesOf(APP, "rename", ["ctrl+x"])).toEqual([]);
  });
});

describe("actionsSharingShortcuts", () => {
  it("names the other actions that share one of the action's shortcuts", () => {
    const chordsOf = (action: { chords: string[] }): readonly string[] => action.chords;
    expect(actionsSharingShortcuts(APP_SHARING_F2, "rename", chordsOf).map((action) => action.id)).toEqual(["delete"]);
    expect(actionsSharingShortcuts(APP_SHARING_F2, "new-folder", chordsOf)).toEqual([]);
    expect(actionsSharingShortcuts(APP, "rename", chordsOf)).toEqual([]);
  });
});

describe("reservedChordNotice", () => {
  it("warns about a chord Imbue Studio or the browser takes, on the platform that takes it", () => {
    expect(reservedChordNotice("meta+w", true)).toContain("Command+W");
    expect(reservedChordNotice("ctrl+w", false)).toContain("Ctrl+W");
    expect(reservedChordNotice("meta+w", false)).toBeNull();
    expect(reservedChordNotice("ctrl+shift+n", true)).toBeNull();
  });
});

describe("unsentEntryLabel", () => {
  it("names an unsent save by its app's display name and its action's label, falling back to the ids", () => {
    const loaded = catalogOf();
    expect(unsentEntryLabel(loaded, { app: "files", action: "rename", chords: [] })).toBe(
      "Files: Rename the selected entry",
    );
    expect(unsentEntryLabel(loaded, { app: "gone", action: "old", chords: [] })).toBe("gone: old");
  });
});

interface PostedBody {
  readonly url: string;
  readonly body: Record<string, unknown>;
}

/** A fetch answering the catalog read, the save, and the deliver route the way the shell does; records the posts. */
function stubFetch(catalog: KeybindingsCatalog, delivery: KeybindingsDeliveryOutcome | null = null): PostedBody[] {
  const posted: PostedBody[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posted.push({ url, body: JSON.parse(String(init.body)) as Record<string, unknown> });
      }
      let answer: unknown = catalog;
      if (url.endsWith("/api/keybindings/deliver")) answer = delivery;
      else if (url.endsWith("/api/keybindings")) answer = { chords: [], delivery };
      return { ok: true, status: 200, json: async () => answer };
    }),
  );
  return posted;
}

async function flush(): Promise<void> {
  for (let round = 0; round < 5; round += 1) await settle();
  m.redraw.sync();
}

/** Mount the section on a Mac and let its catalog read land. */
async function mountSection(): Promise<HTMLElement> {
  const root = mountView(() => m(KeyboardSettingsSection, { isApple: true }));
  await flush();
  return root;
}

function row(root: HTMLElement, actionId: string): HTMLElement {
  return root.querySelector(`[data-keybinding-action="files.${actionId}"]`) as HTMLElement;
}

function click(element: Element | null): void {
  (element as HTMLElement).click();
  m.redraw.sync();
}

function buttonNamed(root: HTMLElement, text: string): HTMLButtonElement {
  const buttons = Array.from(root.querySelectorAll("button"));
  return buttons.find((button) => button.textContent?.trim() === text) as HTMLButtonElement;
}

function pressInRecorder(root: HTMLElement, init: KeyboardEventInit): void {
  const recorder = root.querySelector(".keybinding-recorder") as HTMLInputElement;
  recorder.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, ...init }));
  m.redraw.sync();
}

describe("the shortcut list", () => {
  afterEach(() => {
    unmountViews();
    vi.unstubAllGlobals();
  });

  it("draws each shortcut as a keycap and an action with none as Off, with no marker before any save", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    const deleteKeycaps = Array.from(row(root, "delete").querySelectorAll("[data-chord]")).map((chord) =>
      chord.getAttribute("aria-label"),
    );
    expect(deleteKeycaps).toEqual(["Forward Delete", "Control+Delete"]);
    expect(row(root, "delete").textContent).toContain("or");
    expect(row(root, "new-folder").querySelector(".keybinding-off")?.textContent).toBe("Off");
    expect(root.querySelector(".keybinding-saved")).toBeNull();
  });

  it("resets a change in this workspace through its route when nothing else uses what it restores", async () => {
    const posted = stubFetch(catalogOf());
    const root = await mountSection();

    expect(row(root, "rename").querySelector(".keybinding-reset")).toBeNull();
    expect(row(root, "new-folder").querySelector(".keybinding-reset")).toBeNull();
    click(row(root, "delete").querySelector(".keybinding-reset"));
    await flush();

    expect(posted).toEqual([{ url: "/api/keybindings/reset", body: { app: "files", action: "delete" } }]);
    expect(row(root, "delete").querySelector(".keybinding-saved")?.textContent).toBe("Saved");
  });

  it("asks before a reset brings back a shortcut another action uses, and on Replace takes it from there", async () => {
    const posted = stubFetch(catalogOf({ apps: [APP_AFTER_REPLACE] }));
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-reset"));

    expect(posted).toEqual([]);
    expect(row(root, "rename").querySelector(".keybinding-clash")?.textContent).toContain(
      'Resetting brings back F2, which "Delete the selection" uses now.',
    );
    click(row(root, "rename").querySelector(".keybinding-replace"));
    await flush();

    expect(posted).toEqual([
      { url: "/api/keybindings", body: { app: "files", action: "delete", chords: ["delete"], scope: "workspace" } },
      { url: "/api/keybindings/reset", body: { app: "files", action: "rename" } },
    ]);
  });

  it("leaves everything as it was when a clashing reset is cancelled", async () => {
    const posted = stubFetch(catalogOf({ apps: [APP_AFTER_REPLACE] }));
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-reset"));
    click(row(root, "rename").querySelector(".keybinding-clash-cancel"));

    expect(posted).toEqual([]);
    expect(root.querySelector(".keybinding-clash")).toBeNull();
  });

  it("flags both rows when one shortcut still runs two actions", async () => {
    stubFetch(catalogOf({ apps: [APP_SHARING_F2] }));
    const root = await mountSection();

    expect(row(root, "rename").querySelector(".keybinding-shared")?.textContent).toContain('"Delete the selection"');
    expect(row(root, "delete").querySelector(".keybinding-shared")?.textContent).toContain(
      '"Rename the selected entry"',
    );
    expect(row(root, "new-folder").querySelector(".keybinding-shared")).toBeNull();
  });

  it("collapses an app's section from its header", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    click(root.querySelector(".keybinding-app-header"));

    expect(root.querySelector("[data-keybinding-action]")).toBeNull();
    expect(root.querySelector(".keybinding-app-header")?.getAttribute("aria-expanded")).toBe("false");
  });
});

describe("editing a shortcut", () => {
  afterEach(() => {
    unmountViews();
    vi.unstubAllGlobals();
  });

  it("saves the shortcuts left after removing one, and marks the action Saved until the section reopens", async () => {
    const posted = stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "delete").querySelector(".keybinding-edit"));
    click(root.querySelector('[aria-label="Remove Control+Delete"]'));
    click(buttonNamed(root, "Only in this workspace"));
    await flush();

    expect(posted).toEqual([
      { url: "/api/keybindings", body: { app: "files", action: "delete", chords: ["delete"], scope: "workspace" } },
    ]);
    expect(root.querySelector(".keybinding-editor")).toBeNull();
    expect(row(root, "delete").querySelector(".keybinding-saved")?.textContent).toBe("Saved");
    unmountViews();
    const reopened = await mountSection();
    expect(reopened.querySelector(".keybinding-saved")).toBeNull();
  });

  it("adds a recorded shortcut and saves it everywhere", async () => {
    const posted = stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "r", ctrlKey: true });
    click(buttonNamed(root, "Everywhere"));
    await flush();

    expect(posted).toEqual([
      {
        url: "/api/keybindings",
        body: { app: "files", action: "rename", chords: ["f2", "ctrl+r"], scope: "account" },
      },
    ]);
  });

  it("records Escape like any other key, since an action may be bound to it", async () => {
    const posted = stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "Escape" });
    click(buttonNamed(root, "Only in this workspace"));
    await flush();

    expect(posted).toEqual([
      {
        url: "/api/keybindings",
        body: { app: "files", action: "rename", chords: ["f2", "escape"], scope: "workspace" },
      },
    ]);
  });

  it("stops listening on the recorder's own Cancel without adding anything", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    click(root.querySelector(".keybinding-recorder-cancel"));

    expect(root.querySelector(".keybinding-recorder")).toBeNull();
    expect(root.querySelectorAll(".keybinding-staged")).toHaveLength(1);
    expect(buttonNamed(root, "Only in this workspace").disabled).toBe(true);
  });

  it("says when the recorder starts and stops listening, to its host and to the shell", async () => {
    stubFetch(catalogOf());
    const changes: boolean[] = [];
    const root = mountView(() =>
      m(KeyboardSettingsSection, {
        isApple: true,
        onRecordingChange: (isRecording: boolean) => changes.push(isRecording),
      }),
    );
    await flush();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    const isRecordingWhileListening = isShortcutRecording();
    pressInRecorder(root, { key: "r", ctrlKey: true });

    expect(changes).toEqual([true, false]);
    expect(isRecordingWhileListening).toBe(true);
    expect(isShortcutRecording()).toBe(false);
  });

  it("moves a shortcut another action holds once Replace is chosen, showing the removal at once", async () => {
    const posted = stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "delete").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "F2" });

    expect(root.querySelector(".keybinding-clash")?.textContent).toContain(
      'F2 already runs "Rename the selected entry".',
    );
    expect(buttonNamed(root, "Only in this workspace").disabled).toBe(true);
    click(root.querySelector(".keybinding-replace"));

    expect(row(root, "rename").querySelector(".keybinding-off")?.textContent).toBe("Off");
    expect(row(root, "rename").querySelector(".keybinding-pending")?.textContent).toBe("Changes when you save");
    expect(root.querySelector(".keybinding-replaced")?.textContent).toBe(
      'F2 will be removed from "Rename the selected entry".',
    );
    click(buttonNamed(root, "Only in this workspace"));
    await flush();

    expect(posted).toEqual([
      { url: "/api/keybindings", body: { app: "files", action: "rename", chords: [], scope: "workspace" } },
      {
        url: "/api/keybindings",
        body: { app: "files", action: "delete", chords: ["delete", "ctrl+backspace", "f2"], scope: "workspace" },
      },
    ]);
  });

  it("drops a clashing shortcut on Cancel", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "delete").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "F2" });
    click(root.querySelector(".keybinding-clash-cancel"));

    expect(root.querySelector(".keybinding-clash")).toBeNull();
    expect(root.querySelectorAll(".keybinding-staged")).toHaveLength(2);
  });

  it("asks before restoring defaults another action uses, and on Replace takes them from there", async () => {
    const posted = stubFetch(catalogOf({ apps: [APP_AFTER_REPLACE] }));
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-restore-default"));

    expect(root.querySelector(".keybinding-clash")?.textContent).toContain(
      'The default includes F2, which "Delete the selection" uses now.',
    );
    click(root.querySelector(".keybinding-replace"));
    click(buttonNamed(root, "Only in this workspace"));
    await flush();

    expect(posted).toEqual([
      { url: "/api/keybindings", body: { app: "files", action: "delete", chords: ["delete"], scope: "workspace" } },
      { url: "/api/keybindings", body: { app: "files", action: "rename", chords: ["f2"], scope: "workspace" } },
    ]);
  });

  it("warns when a staged shortcut is one Imbue Studio or the browser takes", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "w", metaKey: true });

    expect(root.querySelector(".keybinding-reserved")?.textContent).toContain("Command+W");
  });

  it("restores an action's default shortcuts when nothing else uses them", async () => {
    const posted = stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "new-folder").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-restore-default"));
    click(buttonNamed(root, "Everywhere"));
    await flush();

    expect(posted).toEqual([
      {
        url: "/api/keybindings",
        body: { app: "files", action: "new-folder", chords: ["ctrl+shift+n"], scope: "account" },
      },
    ]);
  });

  it("gives a visitor one Save, for their own shortcuts here", async () => {
    stubFetch(catalogOf({ is_owner: false, can_save_everywhere: false }));
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));

    expect(buttonNamed(root, "Save")).toBeDefined();
    expect(buttonNamed(root, "Everywhere")).toBeUndefined();
  });
});

describe("the unsent saves' retry", () => {
  afterEach(() => {
    unmountViews();
    vi.unstubAllGlobals();
  });

  const catalogWithUnsent = catalogOf({ unsent: [{ app: "files", action: "rename", chords: ["ctrl+r"] }] });

  it("shows why a retry left the saves unsent", async () => {
    stubFetch(catalogWithUnsent, {
      status: "UNREACHABLE",
      detail: "Imbue Studio could not be reached; the change stays here.",
    });
    const root = await mountSection();

    click(buttonNamed(root, "Try saving to your account again"));
    await flush();

    expect(root.textContent).toContain("Imbue Studio could not be reached; the change stays here.");
  });

  it("shows nothing once a retry delivered them", async () => {
    stubFetch(catalogWithUnsent, { status: "DELIVERED", detail: "" });
    const root = await mountSection();

    click(buttonNamed(root, "Try saving to your account again"));
    await flush();

    expect(root.textContent).not.toContain("could not be reached");
    expect(root.querySelector("[data-keybinding-unsent]")?.textContent).toContain("Files: Rename the selected entry");
  });
});
