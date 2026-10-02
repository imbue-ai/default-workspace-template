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
    index_entry: { title: description, hook: description.toLowerCase(), is_loaded: true },
  };
}

function documentWith(backups: BackupRetention): NotesDocument {
  return {
    notes_dir: "/home/user/workspace/data/memories",
    index_path: "/home/user/workspace/data/memories/MEMORY.md",
    index: { line_count: 2, loaded_line_count: 2, max_lines: 200, max_bytes: 25600, missing_files: [] },
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

function render(
  backups: BackupRetention = BACKED_UP,
  deleteAnswer: string | null = null,
  document: NotesDocument = documentWith(backups),
): Rendered {
  const deleted: string[] = [];
  const root = mountView(() =>
    m(MemoriesPage, {
      state: { kind: "loaded", document },
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
  it("states up front that the backups keep deleted notes, and spells it out under How memory works", () => {
    const { root } = render();

    expect(root.querySelector(".memories-facts")?.textContent).toContain("Backups keep deleted notes up to 24 months");
    expect(root.textContent).not.toContain("Your workspace's backups also hold these notes");
    click(root, "How memory works");
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

    expect(root.querySelector(".memories-facts")?.textContent).toContain("Not backed up");
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

    expect(root.querySelector(".memories-facts")?.textContent).toContain("Shared with your Claude and pi chats");
    click(root, "How memory works");
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

describe("explaining what is remembered, used and shared", () => {
  it("says briefly when a chat saves a note, with the full rules under How memory works", () => {
    const { root } = render();

    expect(root.textContent).toContain(
      "Chats save a note when you ask them to remember something or correct how they work. Fix or delete anything that's wrong.",
    );
    expect(root.querySelector(".memories-how")).toBeNull();
    click(root, "How memory works");
    expect(root.textContent).toContain(
      "Chats are told to save a note when you ask them to remember something, when you correct how they work, or when you mention something that will still matter later, and never to save passwords or keys",
    );
  });

  it("describes every kind of note, and suggests what to say for the empty ones", () => {
    const { root } = render();
    const group = (type: string): HTMLElement => root.querySelector<HTMLElement>(`[data-group="${type}"]`)!;

    expect(group("USER").textContent).toContain("Who you are: your role, what you know well, where you are.");
    expect(group("PROJECT").textContent).toContain(
      "Nothing yet. Try telling a chat: “Remember that the launch moved to March 3.”",
    );
    expect(group("REFERENCE").querySelectorAll(".memories-note")).toHaveLength(0);
    expect(root.querySelector('[data-group="OTHER"]')).toBeNull();
  });

  it("shows the line each note puts in front of every chat", () => {
    const { root } = render();
    const statuses = Array.from(root.querySelectorAll<HTMLElement>(".memories-index-status"));

    expect(statuses.map((status) => status.textContent)).toEqual([
      "Chats start with: “Is a product designer — is a product designer”",
      "Chats start with: “Prefers metric units — prefers metric units”",
    ]);
    expect(root.querySelector(".memories-attention")).toBeNull();
    click(root, "How memory works");
    expect(root.textContent).toContain("whichever is less; yours is 2 lines long.");
  });

  it("says where the notes go, including the AI provider and GitHub", () => {
    const { root } = render();
    click(root, "How memory works");

    expect(root.textContent).toContain("Where your notes go");
    expect(root.textContent).toContain("In this workspace, and in its backups. They aren't synced to GitHub.");
    expect(root.textContent).toContain(
      "Each chat's AI provider (Anthropic, for Claude chats): the list of summaries with every chat, and a note's full text when a chat opens it.",
    );
  });
});

describe("states of the list chats load", () => {
  const withNotes = (notes: readonly Note[], index: Partial<NotesDocument["index"]> = {}): NotesDocument => ({
    ...documentWith(BACKED_UP),
    notes,
    index: { ...documentWith(BACKED_UP).index, ...index },
  });

  it("with no notes, says nothing is saved and suggests what to say for every kind", () => {
    const { root } = render(BACKED_UP, null, withNotes([], { line_count: 0, loaded_line_count: 0 }));

    expect(root.textContent).toContain("Your chats haven't written anything down yet.");
    expect(root.querySelectorAll(".memories-note")).toHaveLength(0);
    expect(Array.from(root.querySelectorAll("[data-group]")).map((group) => group.getAttribute("data-group"))).toEqual(
      ["USER", "FEEDBACK", "PROJECT", "REFERENCE"],
    );
    expect(root.querySelector(".memories-attention")).toBeNull();
    click(root, "How memory works");
    expect(root.textContent).toContain("Nothing is saved yet, so chats start with an empty list.");
  });

  it("warns about a note that isn't in the list, and one past what chats load", () => {
    const missing = { ...note("cello.md", "Plays the cello", "USER"), index_entry: null };
    const cut = {
      ...note("units.md", "Prefers metric units", "FEEDBACK"),
      index_entry: { title: "Units", hook: "metric", is_loaded: false },
    };
    const { root } = render(BACKED_UP, null, withNotes([missing, cut], { line_count: 250, loaded_line_count: 200 }));

    const statuses = Array.from(root.querySelectorAll<HTMLElement>(".memories-index-status"));
    expect(statuses.map((status) => status.getAttribute("data-status"))).toEqual(["not-listed", "past-limit"]);
    expect(root.querySelector(".memories-attention")?.textContent).toBe(
      "1 note isn't in the list chats start with, so chats are unlikely to use it. Editing a note adds it back. 1 note is past what chats load from that list, so they don't see it.",
    );
  });

  it("says when the list still names notes that no longer exist, even with nothing saved", () => {
    const { root } = render(
      BACKED_UP,
      null,
      withNotes([], { line_count: 1, loaded_line_count: 1, missing_files: ["gone.md"] }),
    );

    expect(root.querySelector(".memories-attention")?.textContent).toBe(
      "The list chats start with still names 1 note that no longer exists.",
    );
    click(root, "How memory works");
    expect(root.textContent).toContain("Nothing is saved, but the list chats start with still has 1 line.");
  });
});
