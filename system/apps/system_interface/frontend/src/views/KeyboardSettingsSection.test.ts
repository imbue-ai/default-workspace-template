// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { KeybindingAppView, KeybindingsCatalog, KeybindingsDeliveryOutcome } from "../model/api";
import { settle } from "../testing/fakeShell";
import {
  KeyboardSettingsSection,
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
      source: "default",
    },
    {
      id: "delete",
      label: "Delete the selection",
      default_chords: ["delete"],
      chords: ["delete", "ctrl+backspace"],
      source: "workspace",
    },
    {
      id: "new-folder",
      label: "New folder",
      default_chords: ["ctrl+shift+n"],
      chords: [],
      source: "account",
    },
  ],
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

  it("draws each chord as keycaps, an action with none as Off, and marks the changed ones", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    const deleteKeycaps = Array.from(row(root, "delete").querySelectorAll("[data-chord]")).map((chord) =>
      chord.getAttribute("aria-label"),
    );
    expect(deleteKeycaps).toEqual(["Forward Delete", "Control+Delete"]);
    expect(row(root, "new-folder").querySelector(".keybinding-off")?.textContent).toBe("Off");
    expect(row(root, "rename").querySelector(".keybinding-changed")).toBeNull();
    expect(row(root, "delete").querySelector(".keybinding-changed")).not.toBeNull();
    expect(row(root, "new-folder").querySelector(".keybinding-changed")).not.toBeNull();
  });

  it("offers Reset only for a change made in this workspace, and resets through its route", async () => {
    const posted = stubFetch(catalogOf());
    const root = await mountSection();

    expect(row(root, "rename").querySelector(".keybinding-reset")).toBeNull();
    expect(row(root, "new-folder").querySelector(".keybinding-reset")).toBeNull();
    click(row(root, "delete").querySelector(".keybinding-reset"));
    await flush();

    expect(posted).toEqual([{ url: "/api/keybindings/reset", body: { app: "files", action: "delete" } }]);
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

  it("saves the chords left after removing one, in this workspace", async () => {
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
  });

  it("adds a recorded chord and saves it everywhere", async () => {
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

  it("cancels the recorder on Escape without adding anything", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "Escape" });

    expect(root.querySelector(".keybinding-recorder")).toBeNull();
    expect(root.querySelectorAll(".keybinding-staged")).toHaveLength(1);
    expect(buttonNamed(root, "Only in this workspace").disabled).toBe(true);
  });

  it("moves a chord another action holds once Replace is chosen, saving both actions", async () => {
    const posted = stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "delete").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "F2" });

    expect(root.querySelector(".keybinding-clash")?.textContent).toContain('already runs "Rename the selected entry"');
    expect(buttonNamed(root, "Only in this workspace").disabled).toBe(true);
    click(root.querySelector(".keybinding-replace"));
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

  it("drops a clashing chord on Cancel", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "delete").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "F2" });
    click(root.querySelector(".keybinding-clash-cancel"));

    expect(root.querySelector(".keybinding-clash")).toBeNull();
    expect(root.querySelectorAll(".keybinding-staged")).toHaveLength(2);
  });

  it("warns when a staged chord is one Imbue Studio or the browser takes", async () => {
    stubFetch(catalogOf());
    const root = await mountSection();

    click(row(root, "rename").querySelector(".keybinding-edit"));
    click(root.querySelector(".keybinding-add"));
    pressInRecorder(root, { key: "w", metaKey: true });

    expect(root.querySelector(".keybinding-reserved")?.textContent).toContain("Command+W");
  });

  it("restores an action's default chords", async () => {
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
