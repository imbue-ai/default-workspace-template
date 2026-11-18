import { afterEach, describe, expect, it, vi } from "vitest";

// Redraw is recorded and apiUrl is identity so the notes URL is predictable.
const { mockRedraw } = vi.hoisted(() => ({ mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({ default: { redraw: mockRedraw } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

const NOTE = {
  note_id: "n1",
  timestamp: "2026-11-18T00:00:00+00:00",
  source: "AI Usage Limits",
  text: "Claude usage 2%",
};

async function freshModule(): Promise<typeof import("./AppNotes")> {
  vi.resetModules();
  mockRedraw.mockClear();
  return import("./AppNotes");
}

function answer(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("app notes", () => {
  it("holds a frame's note once for its chat, and bumps the chat's version on each change", async () => {
    const notes = await freshModule();
    expect(notes.isAppNoteFrame({ type: "app_note" })).toBe(true);
    expect(notes.isAppNoteFrame({ type: "assistant_draft" })).toBe(false);

    notes.applyAppNoteFrame("agent-1", { type: "app_note", ...NOTE });
    notes.applyAppNoteFrame("agent-1", { type: "app_note", ...NOTE });

    // Held without the frame's type, and a repeat of the same note (a reconnect's replay) is not held twice.
    expect(notes.getAppNotes("agent-1")).toEqual([NOTE]);
    expect(notes.getAppNotesVersion("agent-1")).toBe(1);
    expect(mockRedraw).toHaveBeenCalledTimes(1);
    expect(notes.getAppNotes("agent-2")).toEqual([]);
    expect(notes.getAppNotesVersion("agent-2")).toBe(0);
  });

  it("replaces what is held with the chat's notes read from the server", async () => {
    const notes = await freshModule();
    notes.applyAppNoteFrame("agent-1", { type: "app_note", ...NOTE, note_id: "stale" });
    const second = { ...NOTE, note_id: "n2", text: "Claude usage 3%" };
    const fetchMock = vi.fn(async () => answer(200, { notes: [NOTE, second] }));
    vi.stubGlobal("fetch", fetchMock);

    await notes.fetchAppNotes("agent-1");

    expect(fetchMock).toHaveBeenCalledWith("/api/chats/agent-1/app-notes");
    expect(notes.getAppNotes("agent-1")).toEqual([NOTE, second]);
    expect(notes.getAppNotesVersion("agent-1")).toBe(2);
  });

  it("keeps a note that arrived on the stream while the read was in flight", async () => {
    const notes = await freshModule();
    const live = { ...NOTE, note_id: "live", timestamp: "2026-11-18T00:05:00+00:00", text: "Claude usage 4%" };
    let finish!: (response: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>((resolve) => (finish = resolve))),
    );

    const reading = notes.fetchAppNotes("agent-1");
    notes.applyAppNoteFrame("agent-1", { type: "app_note", ...live });
    // The server answered before the live note was added.
    finish(answer(200, { notes: [NOTE] }));
    await reading;

    expect(notes.getAppNotes("agent-1")).toEqual([NOTE, live]);
  });

  it("keeps what is held when the read fails or the server refuses it", async () => {
    const notes = await freshModule();
    notes.applyAppNoteFrame("agent-1", { type: "app_note", ...NOTE });

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => answer(404, { detail: "not found" })),
    );
    await notes.fetchAppNotes("agent-1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network down");
      }),
    );
    await notes.fetchAppNotes("agent-1");

    expect(notes.getAppNotes("agent-1")).toEqual([NOTE]);
    expect(notes.getAppNotesVersion("agent-1")).toBe(1);
  });
});
