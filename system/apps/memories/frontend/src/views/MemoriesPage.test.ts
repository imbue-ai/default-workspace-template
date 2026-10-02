// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { BackupRetention, Note, NotesDocument } from "../models/notes";
import { MemoriesPage } from "./MemoriesPage";

afterEach(() => {
  unmountViews();
});

const BACKED_UP: BackupRetention = {
  is_backed_up: true,
  longest_kept: "24 months",
  schedule: ["hourly for 24 hours", "daily for 30 days", "weekly for 12 weeks", "monthly for 24 months"],
  settings_path: "data/system/backup.toml",
};

function note(
  fileName: string,
  description: string,
  noteType: Note["note_type"],
  source: string | null = "claude",
): Note {
  return {
    file_name: fileName,
    name: fileName.replace(".md", ""),
    description,
    note_type: noteType,
    source,
    body: "Some detail.",
    raw_text: `---\ndescription: ${description}\n---\n\nSome detail.\n`,
    modified_at: "2026-10-01T10:00:00Z",
    version: `1-${fileName.length}`,
    attribution: null,
  };
}

function documentWith(backups: BackupRetention): NotesDocument {
  return {
    notes_dir: "/home/user/workspace/data/memories",
    index_path: "/home/user/workspace/data/memories/MEMORY.md",
    backups,
    notes: [
      note("role.md", "Is a product designer", "USER"),
      note("units.md", "Prefers metric units", "FEEDBACK", "pi-coding"),
    ],
    messages: [],
  };
}

interface Rendered {
  readonly root: HTMLElement;
  readonly deleted: string[];
}

function render(backups: BackupRetention = BACKED_UP, deleteAnswer: string | null = null): Rendered {
  const deleted: string[] = [];
  const root = mountView(() =>
    m(MemoriesPage, {
      state: { kind: "loaded", document: documentWith(backups) },
      onSave: async () => null,
      onDelete: async (target: Note) => {
        deleted.push(target.file_name);
        return deleteAnswer;
      },
    }),
  );
  return { root, deleted };
}

function buttonLabelled(root: HTMLElement, label: string): HTMLButtonElement {
  const button = Array.from(root.querySelectorAll<HTMLButtonElement>("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
  if (button === undefined) throw new Error(`no button labelled ${label}`);
  return button;
}

function click(root: HTMLElement, label: string): void {
  buttonLabelled(root, label).click();
  m.redraw.sync();
}

async function settle(): Promise<void> {
  for (let tick = 0; tick < 5; tick++) await Promise.resolve();
  m.redraw.sync();
}

describe("deleting a note", () => {
  it("states up front that the backups keep notes, including deleted ones", () => {
    const { root } = render();

    expect(root.textContent).toContain(
      "Your workspace's backups also hold these notes, including ones you delete, for up to 24 months.",
    );
  });

  it("asks first, naming the note, that it can't be undone, and the copy the backups keep", () => {
    const { root, deleted } = render();

    click(root, "Delete");

    const text = root.textContent ?? "";
    expect(text).toContain("Delete this note for good?");
    expect(text).toContain('"Is a product designer"');
    expect(text).toContain("This can't be undone.");
    expect(text).toContain("A copy stays in your workspace's backups until they expire, up to 24 months from now.");
    expect(text).toContain("They're told you deleted it and asked not to save it again");
    expect(deleted).toEqual([]);
  });

  it("does nothing on Cancel", () => {
    const { root, deleted } = render();

    click(root, "Delete");
    click(root, "Cancel");

    expect(root.textContent).not.toContain("Delete this note for good?");
    expect(deleted).toEqual([]);
  });

  it("deletes the note it was opened for and says the backups still hold a copy", async () => {
    const { root, deleted } = render();

    Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .filter((button) => button.textContent?.trim() === "Delete")[1]
      .click();
    m.redraw.sync();
    click(root, "Delete for good");
    await settle();

    expect(deleted).toEqual(["units.md"]);
    expect(root.textContent).not.toContain("Delete this note for good?");
    expect(root.querySelector('[role="status"]')?.textContent).toBe(
      "Deleted. A copy stays in your backups for up to 24 months.",
    );
  });

  it("shows the reason when the delete is refused", async () => {
    const refusal = "A chat changed this note while you had it open. Its latest version is shown now.";
    const { root } = render(BACKED_UP, refusal);

    click(root, "Delete");
    click(root, "Delete for good");
    await settle();

    expect(root.querySelector('[role="status"]')?.textContent).toBe(refusal);
  });

  it("says a delete is final in a workspace with no backups", async () => {
    const { root } = render({ ...BACKED_UP, is_backed_up: false });

    expect(root.textContent).toContain("This workspace isn't backed up, so a note you delete is gone for good.");
    click(root, "Delete");
    expect(root.textContent).toContain("This workspace isn't backed up, so no other copy is kept.");
    click(root, "Delete for good");
    await settle();
    expect(root.querySelector('[role="status"]')?.textContent).toBe("Deleted.");
  });
});

describe("who keeps and saved the notes", () => {
  it("says Claude and pi chats share the notes and the other harnesses don't yet", () => {
    const { root } = render();

    expect(root.textContent).toContain("Every Claude and pi chat in this workspace, including new ones");
    expect(root.textContent).toContain("Codex, OpenCode and Antigravity chats. They don't use these notes.");
  });

  it("says which harness saved a note", () => {
    const { root } = render();
    const cards = Array.from(root.querySelectorAll<HTMLElement>(".memories-note"));

    expect(cards[0].textContent).toContain("Who wrote this isn't recorded");
    expect(cards[1].textContent).toContain("Saved by a pi chat");
  });
});

interface SaveCall {
  readonly fileName: string;
  readonly description: string;
  readonly body: string;
  readonly version: string;
}

describe("editing a note", () => {
  function renderEditable(saveAnswer: string | null = null): {
    root: HTMLElement;
    saves: SaveCall[];
    document: { current: NotesDocument };
  } {
    const saves: SaveCall[] = [];
    const document = { current: documentWith(BACKED_UP) };
    const root = mountView(() =>
      m(MemoriesPage, {
        state: { kind: "loaded", document: document.current },
        onSave: async (fileName: string, description: string, body: string, version: string) => {
          saves.push({ fileName, description, body, version });
          return saveAnswer;
        },
        onDelete: async () => null,
      }),
    );
    return { root, saves, document };
  }

  function typeSummary(root: HTMLElement, text: string): void {
    const input = root.querySelector<HTMLInputElement>("input[id^='edit-description-']")!;
    input.value = text;
    input.dispatchEvent(new InputEvent("input", { bubbles: true }));
    m.redraw.sync();
  }

  /** What the page renders after it re-reads the notes and a chat has changed role.md meanwhile. */
  function chatChangedRole(document: { current: NotesDocument }): void {
    document.current = {
      ...document.current,
      notes: document.current.notes.map((current) =>
        current.file_name === "role.md"
          ? { ...current, version: "2-99", description: "Is a design lead", body: "Leads the design team." }
          : current,
      ),
    };
    m.redraw.sync();
  }

  it("saves against the version the edit started from", async () => {
    const { root, saves } = renderEditable();

    click(root, "Edit");
    typeSummary(root, "Is a senior product designer");
    click(root, "Save");
    await settle();

    expect(saves).toEqual([
      { fileName: "role.md", description: "Is a senior product designer", body: "Some detail.", version: "1-7" },
    ]);
  });

  it("shows a chat's change made during the edit, and replaces it only when asked", async () => {
    const { root, saves, document } = renderEditable();

    click(root, "Edit");
    typeSummary(root, "Is a senior product designer");
    chatChangedRole(document);

    expect(root.querySelector(".memories-conflict")?.textContent).toContain(
      "A chat changed this note while you were editing it. Its version now reads:Is a design leadLeads the design team.",
    );
    expect(() => buttonLabelled(root, "Save")).toThrow();
    click(root, "Replace with my version");
    await settle();

    expect(saves).toEqual([
      { fileName: "role.md", description: "Is a senior product designer", body: "Some detail.", version: "2-99" },
    ]);
  });

  it("keeps the draft when the save is refused", async () => {
    const refusal = "A chat changed this note while you had it open, so nothing was changed.";
    const { root } = renderEditable(refusal);

    click(root, "Edit");
    typeSummary(root, "Is a senior product designer");
    click(root, "Save");
    await settle();

    expect(root.querySelector('[role="status"]')?.textContent).toBe(refusal);
    expect(root.querySelector<HTMLInputElement>("input[id^='edit-description-']")?.value).toBe(
      "Is a senior product designer",
    );
  });
});
