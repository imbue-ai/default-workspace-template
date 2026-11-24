// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StartOutcome } from "@imbue/workspace-ui/src/app_contract";

import { fileEntry, folderEntry, httpFailure, memoryStorage } from "../testing/records";
import * as server from "./server";
import type { Connections } from "./connections";
import { askAgentAction } from "./failures";
import { FilesModel, compareEntries, type ConflictChoice, type FilesHost } from "./FilesModel";

vi.mock("./server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./server")>();
  return {
    ...actual,
    listFolder: vi.fn(),
    folderStamps: vi.fn(),
    exists: vi.fn(),
    isGone: vi.fn(),
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
  /** Whether the shell says Imbue Studio opens a bug report here. */
  bugReport: { canReport: boolean };
  /** Whether a shell frames the page. */
  shell: { isFraming: boolean };
}

function harness(): Harness {
  const locations: [string, string, number][] = [];
  const replaceAnswer = { value: "replace" as ConflictChoice };
  const bugReport = { canReport: false };
  const shell = { isFraming: true };
  const host: FilesHost = {
    redraw: vi.fn(),
    redrawNow: vi.fn(),
    storage: memoryStorage(),
    showLocation: (folder, mode, index) => locations.push([folder, mode, index]),
    openFile: vi.fn(),
    confirmDelete: vi.fn(async () => true),
    confirmReplace: vi.fn(async () => replaceAnswer.value),
    askNewName: vi.fn(async () => null),
    hasShell: () => shell.isFraming,
    askAgent: vi.fn(async (): Promise<StartOutcome> => ({ isOpened: true })),
    canReportBug: () => bugReport.canReport,
    reportBug: vi.fn(),
  };
  // A copy: a test that adds to the model's entries must not add to every later test's.
  const model = new FilesModel(host, HOME, { exists: true, entries: [...ENTRIES] });
  return { model, host, locations, replaceAnswer, bugReport, shell };
}

beforeEach(() => {
  vi.mocked(server.listFolder).mockResolvedValue({ exists: true, entries: ENTRIES });
  vi.mocked(server.exists).mockResolvedValue(false);
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
    expect(server.movePath).toHaveBeenCalledWith("/home/b.txt", "/home/c.txt");
    expect(model.rename).toBeNull();
    expect(model.selection).toEqual(["/home/c.txt"]);
  });

  it("keeps the field open with a reason for a name already taken", async () => {
    const { model } = harness();
    model.startRename("/home/b.txt");
    model.setRenameDraft("a.png");
    await model.commitRename();
    expect(server.movePath).not.toHaveBeenCalled();
    expect(model.rename?.error).toBe('"a.png" is already taken.');
  });

  it("follows a renamed folder that is the one on show", async () => {
    const { model } = harness();
    model.startRename("/home/zeta/");
    model.navigate("/home/zeta/sub/", { select: [] });
    model.rename = { path: "/home/zeta/", draft: "eta", error: null, isSaving: false };
    await model.commitRename();
    expect(server.movePath).toHaveBeenCalledWith("/home/zeta/", "/home/eta/");
    expect(model.folder).toBe("/home/eta/sub/");
  });

  it("does not start on an entry read live from a computer, which cannot be renamed there", () => {
    const live = "/__remote/host-mac/Users/kim/";
    const host = harness().host;
    const model = new FilesModel(host, live, { exists: true, entries: [fileEntry(live, "notes.md")] });
    model.startRename(`${live}notes.md`);
    expect(model.rename).toBeNull();
  });
});

describe("moving", () => {
  it("moves nothing into or out of a folder read live from a computer, and says why once a drag", async () => {
    const { model } = harness();
    const refusal = "Moving isn't supported on files shared from your computer.";
    await model.moveInto(["/home/b.txt"], "/__remote/host-mac/Users/kim/");
    const [first] = model.toasts.current();
    expect(first.message).toBe(refusal);
    const fromComputer = ["/__remote/host-mac/Users/kim/notes.md", "/__remote/host-mac/Users/kim/plan.md"];
    await model.moveInto(fromComputer, "/home/zeta/");
    expect(server.movePath).not.toHaveBeenCalled();
    // One note for the drag, naming each item it was of behind its details.
    const [, second, ...rest] = model.toasts.current();
    expect(rest).toEqual([]);
    expect(second.message).toBe(refusal);
    expect(second.details).toBe(
      "Tried to move /__remote/host-mac/Users/kim/notes.md into /home/zeta/\n\n" +
        "Tried to move /__remote/host-mac/Users/kim/plan.md into /home/zeta/",
    );
    // The same drag again says it in a fresh note, which replaces its last rather than stacking beside it.
    await model.moveInto(fromComputer, "/home/zeta/");
    const again = model.toasts.current();
    expect(again).toHaveLength(2);
    expect(again[1].id).not.toBe(second.id);
  });
});

describe("deleting", () => {
  it("deletes what was confirmed and nothing when it was not", async () => {
    const { model, host } = harness();
    model.selection = ["/home/b.txt", "/home/a.png"];
    await model.deleteEntries(model.selection);
    expect(server.deletePath).toHaveBeenCalledTimes(2);
    expect(model.selection).toEqual([]);

    vi.mocked(host.confirmDelete).mockResolvedValueOnce(false);
    await model.deleteEntries(["/home/b.txt"]);
    expect(server.deletePath).toHaveBeenCalledTimes(2);
  });

  it("keeps what could not be deleted selected, and stays in a folder whose delete failed", async () => {
    const { model, locations } = harness();
    vi.mocked(server.deletePath).mockImplementation(async (path) => {
      if (path !== "/home/b.txt") throw httpFailure(500, null);
    });
    model.selection = ["/home/b.txt", "/home/a.png"];
    await model.deleteEntries(model.selection);
    expect(model.selection).toEqual(["/home/a.png"]);
    expect(model.toasts.current().map((toast) => toast.message)).toEqual(['Couldn\'t delete "a.png". Try again.']);

    model.navigate("/home/zeta/");
    await model.deleteEntries(["/home/zeta/"]);
    expect(model.folder).toBe("/home/zeta/");
    expect(locations[locations.length - 1]).toEqual(["/home/zeta/", "push", 1]);
    vi.mocked(server.deletePath).mockReset();
  });
});

describe("creating", () => {
  it("makes an untitled folder clear of the names there and starts naming it", async () => {
    const { model } = harness();
    vi.mocked(server.listFolder).mockResolvedValue({
      exists: true,
      entries: [...ENTRIES, folderEntry(HOME, "untitled folder")],
    });
    model.folders.get(HOME)?.entries.push(folderEntry(HOME, "untitled folder"));
    await model.createFolder();
    expect(server.createFolder).toHaveBeenCalledWith("/home/untitled folder 2/");
  });

  it("asks for the name first in a computer's live share, where nothing can be renamed in place", async () => {
    const live = "/__remote/host-mac/Users/kim/";
    const host = harness().host;
    const model = new FilesModel(host, live, { exists: true, entries: [folderEntry(live, "untitled folder")] });
    vi.mocked(server.listFolder).mockResolvedValue({ exists: true, entries: [] });

    await model.createFolder();
    expect(host.askNewName).toHaveBeenCalledWith("untitled folder 2", true);
    expect(server.createFolder).not.toHaveBeenCalled();

    vi.mocked(host.askNewName).mockResolvedValueOnce("Drafts");
    await model.createFolder();
    expect(server.createFolder).toHaveBeenCalledWith(`${live}Drafts/`);
    expect(model.rename).toBeNull();
    expect(model.selection).toEqual([`${live}Drafts/`]);
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

describe("reloadShown", () => {
  it("lists the open folder again, picking up what arrived in it", async () => {
    const { model } = harness();
    const arrived = fileEntry(HOME, "from-the-mac.txt", { size: 9, mtime: 4 });
    vi.mocked(server.listFolder).mockResolvedValue({ exists: true, entries: [...ENTRIES, arrived] });

    model.reloadShown();

    await vi.waitFor(() =>
      expect(model.visibleEntries(HOME).map((entry) => entry.name)).toContain("from-the-mac.txt"),
    );
  });

  it("lists every column's folder again in the column view, and not one already being listed", async () => {
    const { model } = harness();
    model.setViewMode("columns");
    await vi.waitFor(() => expect(model.folderState("/")?.status).toBe("ready"));
    vi.mocked(server.listFolder).mockClear();
    vi.mocked(server.listFolder).mockReturnValue(new Promise(() => {}));

    model.reloadShown();
    model.reloadShown();

    expect(vi.mocked(server.listFolder).mock.calls.map(([folder]) => folder)).toEqual(["/", HOME]);
  });
});

describe("reloadChangedShown", () => {
  it("fetches a folder again only when its stamp is not the one it was fetched at, and never one on a computer", async () => {
    const { model } = harness();
    const stamps = vi.mocked(server.folderStamps);
    const listed = vi.mocked(server.listFolder);
    listed.mockClear();
    listed.mockResolvedValue({ exists: true, entries: ENTRIES, stamp: "1:100" });

    // The page came with no stamp, so the first check fetches it again, and that fetch takes the stamp.
    stamps.mockResolvedValue({ [HOME]: "1:100" });
    await model.reloadChangedShown();
    await vi.waitFor(() => expect(listed).toHaveBeenCalledTimes(1));
    await model.reloadChangedShown();
    expect(listed).toHaveBeenCalledTimes(1);

    // Something landed in it.
    stamps.mockResolvedValue({ [HOME]: "1:200" });
    await model.reloadChangedShown();
    expect(listed).toHaveBeenCalledTimes(2);

    model.navigate("/__remote/host-mac/Users/kim/notes/");
    stamps.mockClear();
    await model.reloadChangedShown();
    expect(stamps).not.toHaveBeenCalled();
  });
});

describe("loading a folder", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("never shows a load that a newer one took over as failed", async () => {
    const actual = await vi.importActual<typeof import("./server")>("./server");
    vi.mocked(server.listFolder).mockImplementation(actual.listFolder);
    const listings: ((listing: object) => void)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        if (url === "/__files/api/connections") return Promise.resolve(new Response('{"devices":[],"shares":[]}'));
        // As fetch does: an aborted request rejects with an AbortError.
        return new Promise<Response>((resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
          listings.push((listing) => resolve(new Response(JSON.stringify(listing))));
        });
      }),
    );
    const { model } = harness();

    const first = model.load("/home/zeta/");
    const second = model.load("/home/zeta/");
    await first;
    expect(model.folderState("/home/zeta/")).toMatchObject({ status: "loading", error: null });

    listings[1]({
      kind: "folder",
      entries: [{ name: "c.txt", is_folder: false, is_symlink: false, size: 1, mtime: 1 }],
    });
    await second;
    expect(model.folderState("/home/zeta/")).toMatchObject({ status: "ready", error: null });
    expect(model.visibleEntries("/home/zeta/").map((entry) => entry.name)).toEqual(["c.txt"]);
  });
});

describe("loadConnections", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("answers a second ask with the read already on its way", async () => {
    const asked: { url: string; answer: (connections: Connections) => void }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (url: string) =>
          new Promise<Response>((resolve) =>
            asked.push({
              url,
              answer: (connections) =>
                resolve(
                  new Response(JSON.stringify(connections), { headers: { "Content-Type": "application/json" } }),
                ),
            }),
          ),
      ),
    );
    // Opening the model starts the first read.
    const { model } = harness();
    const second = model.loadConnections();
    expect(asked.map((ask) => ask.url)).toEqual(["/__files/api/connections"]);

    const listed: Connections = { devices: [{ id: "host-mac", name: "Kims-Mac.local" }], shares: [] };
    asked[0].answer(listed);
    await second;
    expect(model.connections).toEqual(listed);
    // Once it is in, the next ask reads again.
    const third = model.loadConnections();
    expect(asked).toHaveLength(2);
    asked[1].answer(listed);
    await third;
  });
});

describe("telling a failed change", () => {
  const PERMISSION = "You don't have permission to change this.";
  const PERMISSION_DETAIL = "MOVE /home/b.txt: [Errno 13] Permission denied: '/home/zeta/b.txt'";
  const MOVE_TRIED = "Tried to move /home/b.txt into /home/zeta/";
  const GONE = { detail: "It's no longer there.", user_facing_message: "It's no longer there.", kind: "not_found" };

  it("says what failed, then the cause the server gave, with what was tried and the status behind See details", async () => {
    const { model } = harness();
    const said = "Something with that name is already there.";
    vi.mocked(server.movePath).mockRejectedValueOnce(
      httpFailure(412, { detail: said, user_facing_message: said, kind: "already_exists" }),
    );

    await model.moveInto(["/home/b.txt"], "/home/zeta/");

    const message = `Couldn't move "b.txt" into "zeta". ${said}`;
    // The reader drops a reason that only repeats the sentence.
    expect(model.toasts.current().map((toast) => [toast.message, toast.details])).toEqual([
      [message, `${MOVE_TRIED}\nHTTP 412`],
    ]);
  });

  it("takes a delete of an item that is already gone as done: no note, and the folder read again", async () => {
    const { model } = harness();
    vi.mocked(server.deletePath).mockRejectedValueOnce(httpFailure(404, GONE));
    model.selection = ["/home/b.txt"];

    await model.deleteEntries(["/home/b.txt"]);

    expect(model.toasts.current()).toEqual([]);
    expect(model.selection).toEqual([]);
    expect(server.listFolder).toHaveBeenCalledWith(HOME, expect.anything());
  });

  it("says an item a move found gone isn't there anymore, as a note that offers no agent", async () => {
    const { model } = harness();
    vi.mocked(server.movePath).mockRejectedValueOnce(httpFailure(404, GONE));

    await model.moveInto(["/home/b.txt"], "/home/zeta/");

    expect(model.toasts.current().map((toast) => [toast.message, toast.isLasting, toast.isAgentOffered])).toEqual([
      ['"b.txt" isn\'t there anymore. It may have been moved or deleted.', false, false],
    ]);
    expect(server.listFolder).toHaveBeenCalledWith(HOME, expect.anything());
  });

  it("says which folder refused a move that could not be written", async () => {
    const { model } = harness();
    const said = "The folder it would move into is read-only.";
    vi.mocked(server.movePath).mockRejectedValueOnce(
      httpFailure(403, { detail: PERMISSION_DETAIL, user_facing_message: said, kind: "destination_not_writable" }),
    );

    await model.moveInto(["/home/b.txt"], "/home/zeta/");

    expect(model.toasts.current().map((toast) => toast.message)).toEqual([
      'Couldn\'t move "b.txt" into "zeta". That folder is read-only.',
    ]);
  });

  it("says the folder on show is read-only when something can't be created in it", async () => {
    const { model } = harness();
    vi.mocked(server.createFolder).mockRejectedValueOnce(
      httpFailure(403, { detail: "MKCOL /home/untitled folder", user_facing_message: PERMISSION, kind: "read_only" }),
    );

    await model.createFolder();

    expect(model.toasts.current().map((toast) => toast.message)).toEqual([
      'Couldn\'t create "untitled folder". This folder is read-only.',
    ]);
  });

  it("keeps an out-of-space refusal up and asks an agent in the main chat to free up space, in place of the toasts' own ask", async () => {
    const { model, host } = harness();
    const said = "This workspace is out of disk space.";
    vi.mocked(server.createFolder).mockRejectedValueOnce(
      httpFailure(507, { detail: "MKCOL: [Errno 28]", user_facing_message: said, kind: "out_of_space" }),
    );

    await model.createFolder();

    const [toast] = model.toasts.current();
    expect(toast.message).toBe(`Couldn't create "untitled folder". ${said}`);
    expect([toast.isLasting, toast.isHelpOwed, toast.isAgentOffered]).toEqual([true, false, false]);
    expect(toast.actions.map((action) => action.label)).toEqual(["Ask an agent to free up space"]);
    toast.actions[0].run();
    // The shell writes the assist command; the task says, as Imbue Studio, what the user saw and what to do instead
    // of finding a fault.
    const [task, options, delivery] = vi.mocked(host.askAgent).mock.calls[0];
    expect(task).toMatch(
      /^Imbue Studio showed the user this failure: Couldn't create "untitled folder"\. This workspace is out of disk space\.\n\n/,
    );
    expect(task).toContain("Tried to create the folder /home/untitled folder/\nHTTP 507\nMKCOL: [Errno 28]");
    expect(task).toMatch(/\n\nFree up disk space[^\n]*$/);
    expect(options).toMatchObject({ showsOutcome: true });
    expect(delivery).toBe("main-chat");
  });

  it("reads Asking an agent… on the free-up-space button until the chat opens, and offers a refused ask as a draft", async () => {
    const { model, host } = harness();
    const answers: ((outcome: StartOutcome) => void)[] = [];
    const lateAnswers: ((outcome: StartOutcome) => void)[] = [];
    vi.mocked(host.askAgent).mockImplementationOnce((_text, options) => {
      lateAnswers.push(options?.onLateAnswer ?? (() => undefined));
      return new Promise((resolve) => answers.push(resolve));
    });
    vi.mocked(server.createFolder).mockRejectedValueOnce(
      httpFailure(507, {
        detail: "MKCOL",
        user_facing_message: "This workspace is out of disk space.",
        kind: "out_of_space",
      }),
    );
    await model.createFolder();
    const [freeUpSpace] = model.toasts.current()[0].actions;

    freeUpSpace.run();
    expect(freeUpSpace.pending?.isPending()).toBe(true);
    expect(freeUpSpace.pending?.label).toBe("Asking an agent…");

    answers[0]({
      isOpened: false,
      failure: { message: "Chat couldn't open that.", details: "HTTP 400", isHelpOwed: true },
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(freeUpSpace.pending?.isPending()).toBe(false);
    const refused = model.toasts.current().find((toast) => toast.message === "Chat couldn't open that.");
    // The refused ask owes help of its own: the bug report in the agent's place, and the same request as a draft.
    expect([refused?.details, refused?.isHelpOwed, refused?.isAgentOffered]).toEqual(["HTTP 400", true, false]);
    expect(refused?.actions.map((action) => action.label)).toEqual(["Draft it in a chat"]);
    refused?.actions[0].run();
    const [asked, drafted] = vi.mocked(host.askAgent).mock.calls;
    expect([drafted[0], drafted[2]]).toEqual([asked[0], "draft"]);

    // The shell's real answer, after a restart settled the ask, replaces what was said.
    lateAnswers[0]({ isOpened: true });
    expect(model.toasts.current().map((toast) => toast.message)).not.toContain("Chat couldn't open that.");
    expect(host.redraw).toHaveBeenCalled();
  });

  it("says why a folder couldn't be listed, with the folder and the details kept", async () => {
    const { model } = harness();
    vi.mocked(server.listFolder).mockRejectedValueOnce(
      httpFailure(
        403,
        { detail: "GET /home/zeta/: [Errno 13]", user_facing_message: PERMISSION, kind: "permission_denied" },
        "read",
      ),
    );

    await model.load("/home/zeta/");

    expect(model.folderState("/home/zeta/")?.error).toMatchObject({
      message: "Couldn't open this folder. You don't have permission to open it.",
      details: "Tried to open the folder /home/zeta/\nHTTP 403\nGET /home/zeta/: [Errno 13]",
      isHelpOwed: true,
    });
  });

  it("names a move the server could not carry out, and keeps it up with the details as a note that owes help", async () => {
    const { model } = harness();
    vi.mocked(server.movePath).mockRejectedValueOnce(
      httpFailure(500, { detail: "MOVE /home/b.txt: [Errno 5] Input/output error" }),
    );

    await model.moveInto(["/home/b.txt"], "/home/zeta/");

    const [toast] = model.toasts.current();
    expect(toast.message).toBe('Couldn\'t move "b.txt" into "zeta". Try again.');
    expect(toast.isLasting).toBe(true);
    expect(toast.isHelpOwed).toBe(true);
    expect(toast.details).toBe(`${MOVE_TRIED}\nHTTP 500\nMOVE /home/b.txt: [Errno 5] Input/output error`);
  });

  it("offers one agent for several items one change failed on, its task naming each", async () => {
    const { model, host } = harness();
    vi.mocked(server.deletePath)
      .mockRejectedValueOnce(httpFailure(500, { detail: "DELETE /home/b.txt: [Errno 5]" }))
      .mockRejectedValueOnce(httpFailure(500, { detail: "DELETE /home/a.png: [Errno 5]" }));
    await model.deleteEntries(["/home/b.txt", "/home/a.png"]);
    const toasts = model.toasts.current();
    expect(toasts.map((toast) => toast.isAgentOffered)).toEqual([true]);

    await askAgentAction(host)?.({ message: toasts[0].message, details: toasts[0].details });

    const [task] = vi.mocked(host.askAgent).mock.calls[0];
    expect(task).toContain('Couldn\'t delete "b.txt".');
    expect(task).toContain('Couldn\'t delete "a.png".');
    expect(task).toContain("Tried to delete /home/b.txt\nHTTP 500\nDELETE /home/b.txt: [Errno 5]");
    expect(task).toContain("Tried to delete /home/a.png\nHTTP 500\nDELETE /home/a.png: [Errno 5]");
  });

  it.each([
    ["a read-only folder", 403, "destination_not_writable"],
    ["a name already taken", 412, "already_exists"],
    ["no permission", 403, "permission_denied"],
    ["a read-only file system", 403, "read_only"],
    ["a computer that shares read-only", 403, "shared_read_only"],
  ])(
    "offers an agent for a refusal with a known cause, told what was being done and where: %s",
    async (_cause, status, kind) => {
      const { model, host } = harness();
      vi.mocked(server.movePath).mockRejectedValueOnce(
        httpFailure(status, { detail: "MOVE /home/b.txt", user_facing_message: "The server said why.", kind }),
      );

      await model.moveInto(["/home/b.txt"], "/home/zeta/");

      // No button of Files' own: the page's toasts draw "Ask an agent to fix this" on a toast that offers one.
      const [toast] = model.toasts.current();
      expect([toast.isLasting, toast.isAgentOffered, toast.actions]).toEqual([true, true, []]);
      await askAgentAction(host)?.({ message: toast.message, details: toast.details });
      // The chat about Files takes it, with the operation as its task in place of a fault to find.
      expect(host.askAgent).toHaveBeenCalledTimes(1);
      const [task, , delivery] = vi.mocked(host.askAgent).mock.calls[0];
      expect(task).toMatch(/^Imbue Studio showed the user this failure: Couldn't move "b\.txt" into "zeta"\./);
      expect(task).toContain(`${MOVE_TRIED}\nHTTP ${status}\nMOVE /home/b.txt`);
      expect(task).toMatch(/\n\nDo what the user was trying to do if you can; otherwise [^\n]*$/);
      expect(delivery).toBeUndefined();
    },
  );

  it("offers an agent when the name is already taken where a move would land", async () => {
    const { model } = harness();
    vi.mocked(server.exists).mockResolvedValueOnce(true);

    await model.moveInto(["/home/b.txt"], "/home/zeta/");

    expect(server.movePath).not.toHaveBeenCalled();
    expect(model.toasts.current().map((toast) => [toast.message, toast.details, toast.isAgentOffered])).toEqual([
      ['zeta already has an item named "b.txt".', MOVE_TRIED, true],
    ]);
  });

  it("offers no agent for a refusal the server calls expected, which passes on its own", async () => {
    const { model } = harness();
    const said = "It's in use right now. Try again in a moment.";
    vi.mocked(server.movePath).mockRejectedValueOnce(
      httpFailure(409, { detail: "MOVE /home/b.txt", user_facing_message: said, kind: "in_use", expected: true }),
    );

    await model.moveInto(["/home/b.txt"], "/home/zeta/");

    expect(model.toasts.current().map((toast) => [toast.message, toast.isLasting, toast.isAgentOffered])).toEqual([
      [`Couldn't move "b.txt" into "zeta". ${said}`, false, false],
    ]);
  });

  it("says once that nothing answered a delete of several, never that nothing changed", async () => {
    const { model } = harness();
    vi.mocked(server.deletePath).mockRejectedValue(httpFailure(0, null));

    await model.deleteEntries(["/home/b.txt", "/home/a.png"]);

    expect(model.toasts.current().map((toast) => [toast.message, toast.isLasting])).toEqual([
      ["The workspace didn't answer. This may not have gone through.", false],
    ]);
  });

  it("says why a rename failed in the name field, naming the folder that refused it, and reads the folder again", async () => {
    const { model } = harness();
    vi.mocked(server.movePath).mockRejectedValueOnce(
      httpFailure(403, {
        detail: PERMISSION_DETAIL,
        user_facing_message: PERMISSION,
        kind: "destination_not_writable",
      }),
    );
    model.startRename("/home/b.txt");
    model.setRenameDraft("c.txt");

    await model.commitRename();

    expect(model.rename).toMatchObject({
      path: "/home/b.txt",
      error: 'Couldn\'t rename "b.txt" to "c.txt". The folder it\'s in is read-only.',
      isSaving: false,
    });
    // The name field has no room for a button, so a refusal said there offers no agent.
    expect(model.toasts.current()).toEqual([]);
    expect(server.listFolder).toHaveBeenCalledWith(HOME, expect.anything());
  });

  it("ends a rename of an item that is gone, says so in a note, and reads the folder again", async () => {
    const { model } = harness();
    vi.mocked(server.movePath).mockRejectedValueOnce(httpFailure(404, GONE));
    model.startRename("/home/b.txt");
    model.setRenameDraft("c.txt");

    await model.commitRename();

    expect(model.rename).toBeNull();
    expect(model.toasts.current().map((toast) => [toast.message, toast.isLasting])).toEqual([
      ['"b.txt" isn\'t there anymore. It may have been moved or deleted.', false],
    ]);
    expect(server.listFolder).toHaveBeenCalledWith(HOME, expect.anything());
  });

  it("ends a rename that failed for no reason of the server's and keeps it up as a note that owes help", async () => {
    const { model } = harness();
    vi.mocked(server.movePath).mockRejectedValueOnce(httpFailure(500, { detail: "MOVE /home/b.txt: [Errno 5]" }));
    model.startRename("/home/b.txt");
    model.setRenameDraft("c.txt");

    await model.commitRename();

    expect(model.rename).toBeNull();
    const [toast] = model.toasts.current();
    expect(toast.message).toBe('Couldn\'t rename "b.txt" to "c.txt". Try again.');
    expect(toast.isLasting).toBe(true);
    expect(toast.isHelpOwed).toBe(true);
    expect(toast.details).toBe("Tried to rename /home/b.txt to /home/c.txt\nHTTP 500\nMOVE /home/b.txt: [Errno 5]");
  });
});

describe("telling a download that did not start", () => {
  const NOTE = "Couldn't download \"b.txt\". You don't have permission to open this.";

  it("says the download's own words when the item is still there", async () => {
    const { model } = harness();
    vi.mocked(server.isGone).mockResolvedValueOnce(false);

    await model.tellDownloadFailure("/home/b.txt", NOTE);

    expect(model.toasts.current().map((toast) => toast.message)).toEqual([NOTE]);
    expect(server.listFolder).not.toHaveBeenCalled();
  });

  it("says an item the server calls gone isn't there anymore, and reads its folder again", async () => {
    const { model } = harness();
    vi.mocked(server.isGone).mockResolvedValueOnce(true);

    await model.tellDownloadFailure("/home/b.txt", "Couldn't download \"b.txt\". It's no longer there.");

    expect(model.toasts.current().map((toast) => toast.message)).toEqual([
      '"b.txt" isn\'t there anymore. It may have been moved or deleted.',
    ]);
    expect(server.listFolder).toHaveBeenCalledWith(HOME, expect.anything());
  });
});
