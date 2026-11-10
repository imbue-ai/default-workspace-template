// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChordPlatform } from "@imbue/workspace-ui/src/keybindings";
import type { KeybindingAppView, KeybindingsCatalog, KeybindingsDeliveryOutcome } from "../model/api";
import { isShortcutRecording } from "../model/shortcutRecording";
import { settle } from "../testing/fakeShell";
import {
  KeyboardSettingsSection,
  REFUSED_CHORD_NOTICE,
  WORKSPACE_WIDE_CHORD_NOTICE,
  actionsSharingShortcuts,
  clashCandidates,
  clashesOf,
  hiddenChordsHint,
  isResetOffered,
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
      scope: "app",
    },
    {
      id: "delete",
      label: "Delete the selection",
      default_chords: ["delete"],
      chords: ["delete", "ctrl+backspace"],
      reset_chords: ["delete"],
      source: "workspace",
      scope: "app",
    },
    {
      id: "new-folder",
      label: "New folder",
      default_chords: ["ctrl+shift+n"],
      chords: [],
      reset_chords: [],
      source: "account",
      scope: "app",
    },
  ],
};

/** The shell's workspace-wide launcher action beside an app-local one of its own. */
const SHELL: KeybindingAppView = {
  app: "system_interface",
  display_name: "Workspace",
  actions: [
    {
      id: "open-launcher",
      label: "Open the launcher",
      default_chords: ["meta+k"],
      chords: ["meta+k"],
      reset_chords: ["meta+k"],
      source: "default",
      scope: "workspace",
    },
    {
      id: "launcher-secondary-send",
      label: "Launcher: send the typed text to a chosen chat",
      default_chords: ["meta+enter"],
      chords: ["meta+enter"],
      reset_chords: ["meta+enter"],
      source: "default",
      scope: "app",
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

describe("clashesOf", () => {
  it("finds every other action holding a chord, and never the action itself", () => {
    expect(clashesOf(APP, "rename", ["ctrl+backspace"]).map((entry) => entry.otherAction.id)).toEqual(["delete"]);
    expect(clashesOf(APP, "delete", ["ctrl+backspace"])).toEqual([]);
    expect(
      clashesOf(APP_SHARING_F2, "new-folder", ["f2"]).map((entry) => [entry.chord, entry.otherAction.id]),
    ).toEqual([
      ["f2", "rename"],
      ["f2", "delete"],
    ]);
  });

  it("reads an action's chords from the edits made so far, ahead of what it was loaded with", () => {
    const afterReplace = new Map([["delete", ["delete"]]]);
    expect(clashesOf(APP, "rename", ["ctrl+backspace"], afterReplace)).toEqual([]);
  });

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

describe("isResetOffered", () => {
  it("offers a reset only for a change in this workspace that differs from what the reset restores", () => {
    const [rename, deleteAction, newFolder] = APP.actions;
    expect(isResetOffered(deleteAction, "mac")).toBe(true);
    expect(isResetOffered(rename, "mac")).toBe(false);
    expect(isResetOffered(newFolder, "mac")).toBe(false);
    expect(isResetOffered({ ...deleteAction, chords: ["delete"] }, "mac")).toBe(false);
    expect(isResetOffered({ ...deleteAction, chords: ["delete"], reset_chords: ["delete", "f2"] }, "mac")).toBe(true);
  });

  it("compares only the shortcuts that can arrive on the platform", () => {
    // Delete's change adds ctrl+backspace, a Mac's Control chord: off a Mac the change shows as the default does.
    const deleteAction = APP.actions[1];
    expect(isResetOffered(deleteAction, "windows")).toBe(false);
    expect(isResetOffered(deleteAction, "mac")).toBe(true);
  });
});

describe("clashCandidates", () => {
  it("lets an app's own action collide with its app and with every workspace-wide action, and a workspace-wide one with everything", () => {
    const loaded = catalogOf({ apps: [SHELL, APP] });
    const keys = (app: KeybindingAppView, actionId: string): string[] =>
      clashCandidates(loaded, app, actionId).map((candidate) => `${candidate.app.app}.${candidate.action.id}`);
    expect(keys(APP, "rename")).toEqual(["system_interface.open-launcher", "files.delete", "files.new-folder"]);
    expect(keys(SHELL, "open-launcher")).toEqual([
      "system_interface.launcher-secondary-send",
      "files.rename",
      "files.delete",
      "files.new-folder",
    ]);
    expect(keys(SHELL, "launcher-secondary-send")).toEqual(["system_interface.open-launcher"]);
  });
});

describe("clashesOf on a platform", () => {
  it("counts only the chords that can arrive there, on either side", () => {
    // Delete holds ctrl+backspace, a Mac's Control chord: off a Mac it runs nothing, so it clashes with nothing.
    expect(clashesOf(APP, "rename", ["ctrl+backspace"], new Map(), "mac")).toHaveLength(1);
    expect(clashesOf(APP, "rename", ["ctrl+backspace"], new Map(), "windows")).toEqual([]);
  });
});

describe("hiddenChordsHint", () => {
  it("names a Mac's Control shortcut with the Mac's symbols off a Mac, where Ctrl is another key", () => {
    expect(hiddenChordsHint(["ctrl+k"], "windows")).toBe("⌃K is set, but not available on Windows.");
    expect(hiddenChordsHint(["ctrl+k", "ctrl+r"], "linux")).toBe("⌃K, ⌃R are set, but not available on Linux.");
  });
});

describe("unsentEntryLabel", () => {
  it("names an unsent save by its app's display name and its action's label, falling back to the ids", () => {
    const loaded = catalogOf();
    expect(unsentEntryLabel(loaded, { app: "files", action: "rename", chords: [], restored_chords: [] })).toBe(
      "Files: Rename the selected entry",
    );
    expect(unsentEntryLabel(loaded, { app: "gone", action: "old", chords: [], restored_chords: [] })).toBe(
      "gone: old",
    );
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

// What records a chord the desktop app relays, as the last mounted section handed it over.
let recordRelayedChord: ((chord: string) => void) | null = null;

/** Mount the section on a Mac (unless told otherwise) and let its catalog read land. */
async function mountSection(platform: ChordPlatform = "mac"): Promise<HTMLElement> {
  const root = mountView(() =>
    m(KeyboardSettingsSection, {
      platform,
      onChordRelay: (record) => {
        recordRelayedChord = record;
      },
    }),
  );
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
        platform: "mac",
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

  it("takes a recorded shortcut off every action holding it on Replace", async () => {
    const posted = stubFetch(catalogOf({ apps: [APP_SHARING_F2] }));
    const root = await mountSection();

    click(row(root, "new-folder").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "F2" });

    const clashText = root.querySelector(".keybinding-clash")?.textContent ?? "";
    expect(clashText).toContain('"Rename the selected entry"');
    expect(clashText).toContain('"Delete the selection"');
    click(root.querySelector(".keybinding-replace"));
    click(buttonNamed(root, "Only in this workspace"));
    await flush();

    expect(posted).toEqual([
      { url: "/api/keybindings", body: { app: "files", action: "rename", chords: [], scope: "workspace" } },
      { url: "/api/keybindings", body: { app: "files", action: "delete", chords: ["delete"], scope: "workspace" } },
      { url: "/api/keybindings", body: { app: "files", action: "new-folder", chords: ["f2"], scope: "workspace" } },
    ]);
  });

  it("shows what the server holds after a save that fails partway", async () => {
    const posted: string[] = [];
    let catalogReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { action: string };
          posted.push(body.action);
          if (body.action === "delete") return { ok: false, status: 500, json: async () => ({ detail: "disk full" }) };
          return { ok: true, status: 200, json: async () => ({ chords: [], delivery: null }) };
        }
        catalogReads += 1;
        return { ok: true, status: 200, json: async () => catalogOf() };
      }),
    );
    const root = await mountSection();
    const readsBeforeSave = catalogReads;

    click(row(root, "delete").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "F2" });
    click(root.querySelector(".keybinding-replace"));
    click(buttonNamed(root, "Only in this workspace"));
    await flush();

    expect(posted).toEqual(["rename", "delete"]);
    expect(catalogReads).toBe(readsBeforeSave + 1);
    expect(root.querySelector(".keybinding-editor")).toBeNull();
    expect(root.querySelector(".keybinding-pending")).toBeNull();
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

  it("warns in the desktop app when a staged shortcut is one a browser would keep, and saves it", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "w", metaKey: true });

    expect(root.querySelector(".keybinding-warning")?.textContent).toContain(
      "⌘W is a shortcut browsers keep for themselves",
    );
    expect(buttonNamed(root, "Only in this workspace").disabled).toBe(false);
  });

  it("records a key the desktop app took from the OS while the recorder listened, and only then", async () => {
    stubFetch(catalogOf());
    const root = await mountSection("windows");

    click(row(root, "rename").querySelector(".keybinding-edit"));
    recordRelayedChord?.("alt+f4");
    m.redraw.sync();
    click(root.querySelector(".keybinding-add"));
    recordRelayedChord?.("alt+f4");
    m.redraw.sync();

    expect(
      Array.from(root.querySelectorAll(".keybinding-staged [data-chord]")).map((k) => k.getAttribute("data-chord")),
    ).toEqual(["f2", "alt+f4"]);
    expect(root.querySelector(".keybinding-recorder")).toBeNull();
    expect(root.querySelector(".keybinding-warning")?.textContent).toContain("normally used for closing the window");
  });

  it("refuses Ctrl+Alt+Delete and says why, staging nothing", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "Delete", altKey: true, metaKey: true });

    expect(root.querySelector(".keybinding-refused")?.textContent).toBe(REFUSED_CHORD_NOTICE);
    expect(
      Array.from(root.querySelectorAll(".keybinding-staged [data-chord]")).map((k) => k.getAttribute("data-chord")),
    ).toEqual(["f2"]);
  });

  it("refuses a shortcut typing could trigger for a workspace-wide action", async () => {
    stubFetch(catalogOf({ apps: [SHELL, APP] }));
    const root = await mountSection();

    click(root.querySelector('[data-keybinding-action="system_interface.open-launcher"] .keybinding-edit'));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "k", shiftKey: true });

    expect(root.querySelector(".keybinding-refused")?.textContent).toBe(WORKSPACE_WIDE_CHORD_NOTICE);
  });

  it("asks before an app's action takes a workspace-wide shortcut, and on Replace saves both apps", async () => {
    const posted = stubFetch(catalogOf({ apps: [SHELL, APP] }));
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "k", metaKey: true });

    expect(root.querySelector(".keybinding-clash")?.textContent).toContain(
      'Command+K already runs "Open the launcher" (Workspace).',
    );
    click(root.querySelector(".keybinding-replace"));
    expect(
      root.querySelector('[data-keybinding-action="system_interface.open-launcher"] .keybinding-off')?.textContent,
    ).toBe("Off");
    click(buttonNamed(root, "Only in this workspace"));
    await flush();

    expect(posted).toEqual([
      {
        url: "/api/keybindings",
        body: { app: "system_interface", action: "open-launcher", chords: [], scope: "workspace" },
      },
      {
        url: "/api/keybindings",
        body: { app: "files", action: "rename", chords: ["f2", "meta+k"], scope: "workspace" },
      },
    ]);
  });

  it("flags an app's row that shares a shortcut with a workspace-wide action", async () => {
    const files = { ...APP, actions: [{ ...APP.actions[0], chords: ["meta+k"] }] };
    stubFetch(catalogOf({ apps: [SHELL, files] }));
    const root = await mountSection();

    expect(row(root, "rename").querySelector(".keybinding-shared")?.textContent).toContain(
      '"Open the launcher" (Workspace)',
    );
  });

  it("hides a Mac's Control shortcut off a Mac: the row reads Off and names it on hover", async () => {
    const macOnly = { ...APP, actions: [{ ...APP.actions[0], chords: ["ctrl+r"] }, APP.actions[1]] };
    stubFetch(catalogOf({ apps: [macOnly] }));
    const root = await mountSection("windows");

    const off = row(root, "rename").querySelector(".keybinding-off");
    expect(off?.textContent).toBe("Off");
    expect(off?.getAttribute("data-hidden-chords")).toBe("ctrl+r");
    // Delete keeps its one visible shortcut, with no "or" left over for the hidden one.
    const deleteKeycaps = Array.from(row(root, "delete").querySelectorAll("[data-chord]")).map((k) =>
      k.getAttribute("data-chord"),
    );
    expect(deleteKeycaps).toEqual(["delete"]);
    expect(row(root, "delete").textContent).not.toContain("or");
  });

  it("warns under a row about a shortcut a browser would keep, and leaves the warning to the editor while editing", async () => {
    const withMetaW = { ...APP, actions: [{ ...APP.actions[0], chords: ["f2", "meta+w"] }] };
    stubFetch(catalogOf({ apps: [withMetaW] }));
    const root = await mountSection();

    expect(row(root, "rename").querySelector(".keybinding-row-warning")?.textContent).toContain(
      "⌘W is a shortcut browsers keep for themselves",
    );
    click(row(root, "rename").querySelector(".keybinding-edit"));
    expect(row(root, "rename").querySelector(".keybinding-row-warning")).toBeNull();
    expect(row(root, "rename").querySelector(".keybinding-warning")).not.toBeNull();
  });

  it("draws a hidden Mac Control shortcut in the editor with the Mac's symbols", async () => {
    const macOnly = { ...APP, actions: [{ ...APP.actions[0], chords: ["f2", "ctrl+r"] }] };
    stubFetch(catalogOf({ apps: [macOnly] }));
    const root = await mountSection("windows");

    click(row(root, "rename").querySelector(".keybinding-edit"));
    const removeLabels = Array.from(root.querySelectorAll(".keybinding-remove")).map((button) =>
      button.getAttribute("aria-label"),
    );
    expect(removeLabels).toEqual(["Remove F2", "Remove Control+R"]);
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

  const catalogWithUnsent = catalogOf({
    unsent: [{ app: "files", action: "rename", chords: ["ctrl+r"], restored_chords: ["f2"] }],
  });

  it("asks before a discard brings back a shortcut another action uses, and on Replace takes it from there", async () => {
    const posted = stubFetch(
      catalogOf({
        apps: [APP_AFTER_REPLACE],
        unsent: [{ app: "files", action: "rename", chords: [], restored_chords: ["f2"] }],
      }),
    );
    const root = await mountSection();

    click(root.querySelector(".keybinding-discard"));

    expect(posted).toEqual([]);
    expect(root.querySelector("[data-keybinding-unsent] .keybinding-clash")?.textContent).toContain(
      'Discarding brings back F2, which "Delete the selection" uses now.',
    );
    expect(row(root, "rename").querySelector(".keybinding-clash")).toBeNull();
    click(root.querySelector(".keybinding-replace"));
    await flush();

    expect(posted).toEqual([
      { url: "/api/keybindings", body: { app: "files", action: "delete", chords: ["delete"], scope: "workspace" } },
      { url: "/api/keybindings/discard", body: { app: "files", action: "rename" } },
    ]);
  });

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
