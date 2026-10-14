// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Note, NotesDocument } from "./notes";

type NotesModule = typeof import("./notes");

interface Request {
  readonly url: string;
  readonly method: string;
}

const NOTE_CONFLICT = "A chat changed this note while you had it open, so nothing was changed.";

function documentWith(notesDir: string): NotesDocument {
  return {
    notes_dir: notesDir,
    index_path: `${notesDir}/MEMORY.md`,
    backups: { is_backed_up: false, longest_kept: null, schedule: [], settings_path: "data/system/backup.toml" },
    notes: [],
    messages: [],
  };
}

const NOTE: Note = {
  file_name: "role.md",
  name: "role",
  description: "Is a product designer",
  note_type: "USER",
  body: "",
  raw_text: "",
  modified_at: "2026-10-01T10:00:00Z",
  version: "1-7",
  attribution: null,
};

function json(body: unknown, status: number = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** A fresh copy of the module, so each test starts from "loading", and the requests it makes. */
async function withServer(answer: (request: Request) => Promise<Response>): Promise<{
  readonly notes: NotesModule;
  readonly requests: Request[];
}> {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (url: string, init: RequestInit = {}) => {
    const request = { url, method: init.method ?? "GET" };
    requests.push(request);
    return answer(request);
  });
  vi.resetModules();
  return { notes: await import("./notes"), requests };
}

/** Notes read as ``document``; every write answered with ``writeAnswer``. */
function server(writeAnswer: () => Promise<Response>, document: NotesDocument = documentWith("/notes")) {
  return async (request: Request): Promise<Response> => (request.method === "GET" ? json(document) : writeAnswer());
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("writes", () => {
  it("names a note conflict", async () => {
    const { notes } = await withServer(server(async () => json({ detail: "changed" }, 409)));

    expect(await notes.saveNote("role.md", "Is a designer", "", "1-7")).toBe(NOTE_CONFLICT);
    expect(await notes.deleteNote(NOTE)).toBe(NOTE_CONFLICT);
  });

  it("gives the server's reason, or says what the page answered when there is none", async () => {
    const answers = [
      json({ detail: "a note needs a summary" }, 400),
      new Response("<html>oops</html>", { status: 500 }),
    ];
    const { notes } = await withServer(server(async () => answers.shift()!));

    expect(await notes.saveNote("role.md", "", "", "1-7")).toBe("a note needs a summary");
    expect(await notes.saveNote("role.md", "Is a designer", "", "1-7")).toBe("The page answered 500.");
  });

  it("says the app is starting or unreachable rather than showing the raw error", async () => {
    const { notes } = await withServer(server(async () => new Response("starting", { status: 503 })));
    expect(await notes.saveNote("role.md", "Is a designer", "", "1-7")).toBe(
      "Agent Memory is starting. Try again in a moment.",
    );

    const offline = await withServer(server(() => Promise.reject(new TypeError("Failed to fetch"))));
    expect(await offline.notes.deleteNote(NOTE)).toBe("Couldn't reach Agent Memory. Try again in a moment.");
  });

  it("re-reads the notes after every write, whether it worked or not", async () => {
    const answers = [json({}), json({ detail: "no" }, 400)];
    const { notes, requests } = await withServer(server(async () => answers.shift()!));

    await notes.saveNote("role.md", "Is a designer", "", "1-7");
    await notes.deleteNote(NOTE);

    expect(requests.map((request) => `${request.method} ${request.url}`)).toEqual([
      "PUT /api/notes/role.md",
      "GET /api/notes",
      "DELETE /api/notes/role.md",
      "GET /api/notes",
    ]);
    expect(notes.getNotesState().kind).toBe("loaded");
  });

  it("escapes the note's file name in the address", async () => {
    const { notes, requests } = await withServer(server(async () => json({})));

    await notes.saveNote("a b#?/c.md", "x", "", "1-1");
    await notes.deleteNote({ ...NOTE, file_name: "50%.md" });

    expect(requests.filter((request) => request.method !== "GET").map((request) => request.url)).toEqual([
      "/api/notes/a%20b%23%3F%2Fc.md",
      "/api/notes/50%25.md",
    ]);
  });
});

describe("reading the notes", () => {
  it("keeps the notes shown when a later read fails", async () => {
    const answers = [
      async () => json(documentWith("/notes")),
      async () => json({}, 500),
      () => Promise.reject(new TypeError("Failed to fetch")),
    ];
    const { notes } = await withServer(() => answers.shift()!());

    await notes.refreshNotes();
    await notes.refreshNotes();
    await notes.refreshNotes();

    expect(notes.getNotesState()).toEqual({ kind: "loaded", document: documentWith("/notes") });
  });

  it("says why when the first read fails", async () => {
    const { notes } = await withServer(() => Promise.reject(new TypeError("Failed to fetch")));
    await notes.refreshNotes();
    expect(notes.getNotesState()).toEqual({
      kind: "failed",
      message: "Couldn't reach Agent Memory. Try again in a moment.",
    });

    const starting = await withServer(async () => new Response("starting", { status: 503 }));
    await starting.notes.refreshNotes();
    expect(starting.notes.getNotesState()).toEqual({
      kind: "failed",
      message: "Agent Memory is starting. Try again in a moment.",
    });
  });

  it("ignores a read that finishes after a later one", async () => {
    const pending: ((response: Response) => void)[] = [];
    const { notes } = await withServer(() => new Promise((resolve) => pending.push(resolve)));

    const older = notes.refreshNotes();
    const newer = notes.refreshNotes();
    pending[1](json(documentWith("/newer")));
    await newer;
    pending[0](json(documentWith("/older")));
    await older;

    expect(notes.getNotesState()).toEqual({ kind: "loaded", document: documentWith("/newer") });
  });
});
