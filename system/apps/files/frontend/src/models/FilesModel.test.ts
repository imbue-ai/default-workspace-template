// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fileEntry, folderEntry, memoryStorage } from "../testing/records";
import * as dufs from "./dufs";
import { FilesModel, compareEntries, type ConflictChoice, type FilesHost } from "./FilesModel";

vi.mock("./dufs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./dufs")>();
  return {
    ...actual,
    listFolder: vi.fn(),
    searchFolder: vi.fn(),
    exists: vi.fn(),
    movePath: vi.fn(),
    deletePath: vi.fn(),
    createFolder: vi.fn(),
    writeFile: vi.fn(),
    uploadFile: vi.fn(),
  };
});

const HOME = "/home/";
const ENTRIES = [
  fileEntry(HOME, "b.txt", { size: 300, mtime: 1 }),
  folderEntry(HOME, "zeta"),
  fileEntry(HOME, "a.png", { size: 100, mtime: 3 }),
  fileEntry(HOME, ".env", { size: 5, mtime: 2 }),
];

interface Harness {
  model: FilesModel;
  host: FilesHost;
  locations: [string, string, number][];
  replaceAnswer: { value: ConflictChoice };
}

function harness(): Harness {
  const locations: [string, string, number][] = [];
  const replaceAnswer = { value: "replace" as ConflictChoice };
  const host: FilesHost = {
    redraw: vi.fn(),
    redrawNow: vi.fn(),
    storage: memoryStorage(),
    showLocation: (folder, mode, index) => locations.push([folder, mode, index]),
    openFile: vi.fn(),
    confirmDelete: vi.fn(async () => true),
    confirmReplace: vi.fn(async () => replaceAnswer.value),
  };
  const model = new FilesModel(host, HOME, { exists: true, entries: ENTRIES });
  return { model, host, locations, replaceAnswer };
}

beforeEach(() => {
  vi.mocked(dufs.listFolder).mockResolvedValue({ exists: true, entries: ENTRIES });
  vi.mocked(dufs.exists).mockResolvedValue(false);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("arranging a folder", () => {
  it("puts folders first, sorts by name, and hides system files until asked", () => {
    const { model } = harness();
    expect(model.visibleEntries(HOME).map((entry) => entry.name)).toEqual(["zeta", "a.png", "b.txt"]);
    model.toggleHidden();
    expect(model.visibleEntries(HOME).map((entry) => entry.name)).toEqual(["zeta", ".env", "a.png", "b.txt"]);
  });

  it("sorts by a column and flips on a second ask, folders still first", () => {
    const { model } = harness();
    model.sortBy("size");
    expect(model.visibleEntries(HOME).map((entry) => entry.name)).toEqual(["zeta", "a.png", "b.txt"]);
    model.sortBy("size");
    expect(model.preferences.sort).toEqual({ key: "size", descending: true });
    expect(model.visibleEntries(HOME).map((entry) => entry.name)).toEqual(["zeta", "b.txt", "a.png"]);
  });

  it("compares names the way people count", () => {
    const sort = { key: "name" as const, descending: false };
    expect(compareEntries(fileEntry(HOME, "file 2"), fileEntry(HOME, "file 10"), sort)).toBeLessThan(0);
  });

  it("lays columns from the shortcut the folder is under", () => {
    const { model } = harness();
    model.folder = "/home/user/workspace/data/notes/";
    expect(model.columnFolders()).toEqual([
      "/home/user/workspace/",
      "/home/user/workspace/data/",
      "/home/user/workspace/data/notes/",
    ]);
    model.folder = "/etc/ssh/";
    expect(model.columnFolders()).toEqual(["/", "/etc/", "/etc/ssh/"]);
  });

  it("keeps the columns' start when drilling into a folder that is also a shortcut, and restarts from the sidebar", () => {
    const { model } = harness();
    model.navigate("/home/user/workspace/", { startColumns: true });
    model.navigate("/home/user/workspace/data/");
    model.navigate("/home/user/workspace/data/uploads/");
    expect(model.columnFolders()).toEqual([
      "/home/user/workspace/",
      "/home/user/workspace/data/",
      "/home/user/workspace/data/uploads/",
    ]);
    model.navigate("/home/user/workspace/data/uploads/", { startColumns: true });
    expect(model.columnFolders()).toEqual(["/home/user/workspace/data/uploads/"]);
    model.navigate("/etc/");
    expect(model.columnFolders()).toEqual(["/", "/etc/"]);
  });
});

describe("selecting", () => {
  it("picks one, toggles with the modifier, and extends a run from the anchor", () => {
    const { model } = harness();
    const order = model.visibleEntries(HOME);
    model.select("/home/zeta/", "only", order);
    model.select("/home/b.txt", "extend", order);
    expect(model.selection).toEqual(["/home/zeta/", "/home/a.png", "/home/b.txt"]);
    model.select("/home/a.png", "toggle", order);
    expect(model.selection).toEqual(["/home/zeta/", "/home/b.txt"]);
  });

  it("opens a folder chosen in the columns as the next column, replacing the history entry", () => {
    const { model, locations } = harness();
    model.selectInColumn(folderEntry(HOME, "zeta"), "only");
    expect(model.folder).toBe("/home/zeta/");
    expect(model.selection).toEqual(["/home/zeta/"]);
    expect(locations).toEqual([["/home/zeta/", "replace", 0]]);
  });
});

describe("moving around", () => {
  it("goes up to the parent with the folder it came from selected", () => {
    const { model, locations } = harness();
    model.goUp();
    expect(model.folder).toBe("/");
    expect(model.selection).toEqual([HOME]);
    expect(locations).toEqual([["/", "push", 1]]);
    expect(model.canGoBack).toBe(true);
    model.restore(HOME, 0);
    expect(model.canGoBack).toBe(false);
    expect(model.canGoForward).toBe(true);
  });

  it("opens a file on its own page", () => {
    const { model, host } = harness();
    model.open(fileEntry(HOME, "b.txt"));
    expect(host.openFile).toHaveBeenCalledWith("/home/b.txt");
  });
});

describe("renaming", () => {
  it("moves the entry and selects it under its new name", async () => {
    const { model } = harness();
    model.startRename("/home/b.txt");
    model.setRenameDraft("c.txt");
    await model.commitRename();
    expect(dufs.movePath).toHaveBeenCalledWith("/home/b.txt", "/home/c.txt");
    expect(model.rename).toBeNull();
    expect(model.selection).toEqual(["/home/c.txt"]);
  });

  it("keeps the field open with a reason for a name already taken", async () => {
    const { model } = harness();
    model.startRename("/home/b.txt");
    model.setRenameDraft("a.png");
    await model.commitRename();
    expect(dufs.movePath).not.toHaveBeenCalled();
    expect(model.rename?.error).toBe('"a.png" is already taken.');
  });

  it("follows a renamed folder that is the one on show", async () => {
    const { model } = harness();
    model.startRename("/home/zeta/");
    model.navigate("/home/zeta/sub/", { select: [] });
    model.rename = { path: "/home/zeta/", draft: "eta", error: null, isSaving: false };
    await model.commitRename();
    expect(dufs.movePath).toHaveBeenCalledWith("/home/zeta/", "/home/eta/");
    expect(model.folder).toBe("/home/eta/sub/");
  });
});

describe("deleting", () => {
  it("deletes what was confirmed and nothing when it was not", async () => {
    const { model, host } = harness();
    model.selection = ["/home/b.txt", "/home/a.png"];
    await model.deleteEntries(model.selection);
    expect(dufs.deletePath).toHaveBeenCalledTimes(2);
    expect(model.selection).toEqual([]);

    vi.mocked(host.confirmDelete).mockResolvedValueOnce(false);
    await model.deleteEntries(["/home/b.txt"]);
    expect(dufs.deletePath).toHaveBeenCalledTimes(2);
  });
});

describe("creating", () => {
  it("makes an untitled folder clear of the names there and starts naming it", async () => {
    const { model } = harness();
    vi.mocked(dufs.listFolder).mockResolvedValue({
      exists: true,
      entries: [...ENTRIES, folderEntry(HOME, "untitled folder")],
    });
    model.folders.get(HOME)?.entries.push(folderEntry(HOME, "untitled folder"));
    await model.createFolder();
    expect(dufs.createFolder).toHaveBeenCalledWith("/home/untitled folder 2/");
  });
});

describe("uploading", () => {
  function file(name: string): File {
    return new File(["x"], name);
  }

  it("asks about taken names, and keeps both by numbering the arrivals", async () => {
    const { model, host, replaceAnswer } = harness();
    const added = vi.spyOn(model.uploads, "add").mockImplementation(() => undefined);
    replaceAnswer.value = "keep-both";
    await model.uploadInto(HOME, [
      { file: file("b.txt"), relativePath: "b.txt" },
      { file: file("x.png"), relativePath: "zeta/x.png" },
      { file: file("new.md"), relativePath: "new.md" },
    ]);
    expect(host.confirmReplace).toHaveBeenCalledWith(["b.txt", "zeta"]);
    expect(added.mock.calls[0][1].map((source) => source.relativePath)).toEqual(["b 2.txt", "zeta 2/x.png", "new.md"]);
  });

  it("sends nothing when the user cancels, and everything as named when they replace", async () => {
    const { model, replaceAnswer } = harness();
    const added = vi.spyOn(model.uploads, "add").mockImplementation(() => undefined);
    replaceAnswer.value = "cancel";
    await model.uploadInto(HOME, [{ file: file("b.txt"), relativePath: "b.txt" }]);
    expect(added).not.toHaveBeenCalled();
    replaceAnswer.value = "replace";
    await model.uploadInto(HOME, [{ file: file("b.txt"), relativePath: "b.txt" }]);
    expect(added.mock.calls[0][1].map((source) => source.relativePath)).toEqual(["b.txt"]);
  });
});
