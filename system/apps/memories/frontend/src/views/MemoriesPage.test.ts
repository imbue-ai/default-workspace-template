// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { BackupRetention, MemoryControls, Note, NotesDocument } from "../models/notes";
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
    index_entry: { is_loaded: true },
  };
}

function documentWith(backups: BackupRetention): NotesDocument {
  return {
    notes_dir: "/home/user/workspace/data/memories",
    index_path: "/home/user/workspace/data/memories/MEMORY.md",
    index: { line_count: 2, loaded_line_count: 2, max_lines: 200, max_bytes: 25600, missing_files: [] },
    backups,
    controls: { is_paused: false, disabled_harnesses: [] },
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
  readonly savedControls: MemoryControls[];
}

function render(
  backups: BackupRetention = BACKED_UP,
  deleteAnswer: string | null = null,
  document: NotesDocument = documentWith(backups),
): Rendered {
  const deleted: string[] = [];
  const savedControls: MemoryControls[] = [];
  const root = mountView(() =>
    m(MemoriesPage, {
      state: { kind: "loaded", document },
      onSave: async () => null,
      onDelete: async (target: Note) => {
        deleted.push(target.file_name);
        return deleteAnswer;
      },
      onSaveControls: async (controls: MemoryControls) => {
        savedControls.push(controls);
        return null;
      },
    }),
  );
  return { root, deleted, savedControls };
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
    expect(text).toContain(
      "Chats that are open now, and the transcripts of chats that read it, still have it. Open chats are told you deleted it and asked not to save it again.",
    );
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
    const refusal = "A chat changed this note while you had it open, so nothing was changed.";
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

  it("shows each note as its summary, who saved it and its actions, with the full text only on Show more", () => {
    const { root } = render();
    const card = (): HTMLElement => root.querySelectorAll<HTMLElement>(".memories-note")[0];

    expect(card().textContent).toContain("Is a product designer");
    expect(card().textContent).toContain("Who wrote this isn't recorded");
    expect(card().textContent).not.toContain("Some detail.");
    expect(card().textContent).not.toContain("Show file");
    expect(root.textContent).not.toContain("Chats start with");
    expect(root.querySelector(".memories-index-warning")).toBeNull();
    expect(root.querySelector(".memories-attention")).toBeNull();

    click(root, "Show more");
    expect(card().getAttribute("data-expanded")).toBe("true");
    expect(card().textContent).toContain("Some detail.");
    click(root, "Show file");
    expect(card().querySelector("pre")?.textContent).toContain("description: Is a product designer");
    expect(root.querySelectorAll<HTMLElement>(".memories-note")[1].textContent).not.toContain("Some detail.");

    click(root, "Show less");
    expect(card().textContent).not.toContain("Some detail.");
    expect(card().querySelector("pre")).toBeNull();
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

  it("says in the technical details what a delete leaves behind, and that the change record keeps no text", () => {
    const { root } = render();
    click(root, "How memory works");
    click(root, "Technical details");

    expect(root.textContent).toContain(
      "Deleting a note erases its file and its line in /home/user/workspace/data/memories/MEMORY.md. Chat transcripts that read or listed the note still hold its text or summary.",
    );
    expect(root.textContent).toContain("(the note's file name, what was done and when; never its text)");
    expect(root.textContent).toContain("(the first 200 lines, or 25KB, whichever is less)");
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
      index_entry: { is_loaded: false },
    };
    const { root } = render(BACKED_UP, null, withNotes([missing, cut], { line_count: 250, loaded_line_count: 200 }));

    const warnings = Array.from(root.querySelectorAll<HTMLElement>(".memories-index-warning"));
    expect(warnings.map((warning) => warning.textContent)).toEqual([
      "Not in the list chats start with, so chats are unlikely to use it. Edit it to add it back.",
      "Past what chats load from that list, so they don't see it",
    ]);
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

describe("memory settings", () => {
  const withControls = (controls: MemoryControls | null): NotesDocument => ({ ...documentWith(BACKED_UP), controls });
  const switchNamed = (root: HTMLElement, id: string): HTMLButtonElement =>
    root.querySelector<HTMLButtonElement>(`#${id}`)!;

  it("opens switches that pause memory or turn it off for one kind of chat, saying when each takes effect", async () => {
    const { root, savedControls } = render();

    expect(root.querySelector(".memories-settings")).toBeNull();
    click(root, "Settings");
    expect(switchNamed(root, "memory-switch-all").getAttribute("aria-checked")).toBe("true");
    expect(switchNamed(root, "memory-switch-pi_coding").getAttribute("aria-checked")).toBe("true");
    expect(root.querySelector(".memories-settings")?.textContent).toContain(
      "pi chats follow these from their next message. New Claude chats follow them right away",
    );
    expect(root.querySelector(".memories-settings")?.textContent).toContain(
      "Claude chats started while memory was off pick it back up only once restarted.",
    );
    const hint = root.querySelector(`#${switchNamed(root, "memory-switch-all").getAttribute("aria-describedby")}`);
    expect(hint?.textContent).toContain("A chat still knows what you told it earlier in the same conversation.");
    expect(root.querySelector('label[for="memory-switch-all"]')?.textContent).toBe("Use memory");
    expect(switchNamed(root, "memory-switch-claude").hasAttribute("aria-describedby")).toBe(false);

    switchNamed(root, "memory-switch-pi_coding").click();
    await settle();
    switchNamed(root, "memory-switch-all").click();
    await settle();

    expect(savedControls).toEqual([
      { is_paused: false, disabled_harnesses: ["PI_CODING"] },
      { is_paused: true, disabled_harnesses: [] },
    ]);
    expect(root.querySelector('[role="status"]')?.textContent).toContain("Saved. pi chats follow it");
  });

  it("shows which chats use the notes at the top and under How memory works", () => {
    const piOff = render(BACKED_UP, null, withControls({ is_paused: false, disabled_harnesses: ["PI_CODING"] })).root;
    expect(piOff.querySelector(".memories-sharing")?.textContent).toBe("Used by your Claude chats only");
    click(piOff, "How memory works");
    expect(piOff.textContent).toContain("Every Claude chat in this workspace, including new ones.");
    unmountViews();

    const paused = render(BACKED_UP, null, withControls({ is_paused: true, disabled_harnesses: [] })).root;
    expect(paused.querySelector(".memories-sharing")?.textContent).toBe("Memory paused");
    click(paused, "Settings");
    expect(switchNamed(paused, "memory-switch-all").getAttribute("aria-checked")).toBe("false");
    expect(switchNamed(paused, "memory-switch-claude").disabled).toBe(true);
  });

  it("says when the settings can't be read, that chats treat that as off, and replaces them when turned on", async () => {
    const { root, savedControls } = render(BACKED_UP, null, withControls(null));

    expect(root.querySelector(".memories-sharing")?.textContent).toBe("Memory off: settings unreadable");
    click(root, "Settings");
    expect(root.textContent).toContain("The memory settings couldn't be read, so chats treat memory as off.");
    expect(switchNamed(root, "memory-switch-all").getAttribute("aria-checked")).toBe("false");

    switchNamed(root, "memory-switch-all").click();
    await settle();

    expect(savedControls).toEqual([{ is_paused: false, disabled_harnesses: [] }]);
  });
});

interface SaveCall {
  readonly fileName: string;
  readonly description: string;
  readonly body: string;
  readonly version: string;
}

describe("editing a note", () => {
  function renderEditable(answer: () => Promise<string | null> = async () => null): {
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
          return answer();
        },
        onDelete: async () => null,
        onSaveControls: async () => null,
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
    const { root } = renderEditable(async () => refusal);

    click(root, "Edit");
    typeSummary(root, "Is a senior product designer");
    click(root, "Save");
    await settle();

    expect(root.querySelector('[role="status"]')?.textContent).toBe(refusal);
    expect(root.querySelector<HTMLInputElement>("input[id^='edit-description-']")?.value).toBe(
      "Is a senior product designer",
    );
  });

  it("opens the editor with the summary focused, and saves only once something changed", () => {
    const { root } = renderEditable();

    click(root, "Edit");
    const input = root.querySelector<HTMLInputElement>("input[id^='edit-description-']");
    expect(globalThis.document.activeElement).toBe(input);
    expect(buttonLabelled(root, "Save").disabled).toBe(true);

    typeSummary(root, "Is a senior product designer");
    expect(buttonLabelled(root, "Save").disabled).toBe(false);
    typeSummary(root, "Is a product designer");
    expect(buttonLabelled(root, "Save").disabled).toBe(true);
    typeSummary(root, " ");
    expect(buttonLabelled(root, "Save").disabled).toBe(true);
  });

  it("offers to replace a chat's change even when the draft is unchanged", () => {
    const { root, document } = renderEditable();

    click(root, "Edit");
    chatChangedRole(document);

    expect(buttonLabelled(root, "Replace with my version").disabled).toBe(false);
  });

  it("lets only one note be edited at a time", () => {
    const { root } = renderEditable();

    click(root, "Edit");

    expect(buttonLabelled(root, "Edit").disabled).toBe(true);
    click(root, "Cancel");
    expect(
      Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
        .filter((button) => button.textContent?.trim() === "Edit")
        .map((button) => button.disabled),
    ).toEqual([false, false]);
  });

  it("keeps the unsaved text on screen when a chat deletes the note being edited, until dismissed", () => {
    const { root, document } = renderEditable();

    click(root, "Edit");
    typeSummary(root, "Is a senior product designer");
    document.current = {
      ...document.current,
      notes: document.current.notes.filter((current) => current.file_name !== "role.md"),
    };
    m.redraw.sync();

    const panel = root.querySelector<HTMLElement>(".memories-orphaned-draft");
    expect(panel?.textContent).toBe(
      "A chat deleted the note you were editing. Your unsaved text is below; copy anything you want to keep.Is a senior product designerSome detail.Dismiss",
    );
    expect(root.querySelector("input[id^='edit-description-']")).toBeNull();
    expect(root.querySelector(".memories-group")?.previousElementSibling).toBe(panel);
    expect(buttonLabelled(root, "Edit").disabled).toBe(true);

    click(root, "Dismiss");

    expect(root.querySelector(".memories-orphaned-draft")).toBeNull();
    expect(buttonLabelled(root, "Edit").disabled).toBe(false);
  });

  it("disables every action while a save is in flight, and announces the outcome", async () => {
    let finish: (answer: string | null) => void = () => undefined;
    const { root } = renderEditable(() => new Promise((resolve) => (finish = resolve)));
    const status = (): HTMLElement | null => root.querySelector<HTMLElement>('[role="status"]');
    const actions = (): boolean[] =>
      Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
        .filter((button) => ["Save", "Cancel", "Edit", "Delete"].includes(button.textContent?.trim() ?? ""))
        .map((button) => button.disabled);

    expect(status()?.textContent).toBe("");
    click(root, "Edit");
    typeSummary(root, "Is a senior product designer");
    click(root, "Save");

    expect(actions()).toEqual([true, true, true, true]);
    finish(null);
    await settle();

    expect(actions()).toEqual([false, false, false, false]);
    expect(status()?.textContent).toBe("Saved. New chats will use this version.");
  });
});
