import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { httpFailure } from "../testing/records";
import * as server from "./server";
import { FINISHED_LINGER_MS, UploadQueue } from "./uploads";

vi.mock("./server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./server")>()),
  uploadFile: vi.fn(),
}));

/** Each upload's settle functions, in the order they started. */
let settles: { resolve: () => void; reject: (error: Error) => void }[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  settles = [];
  vi.mocked(server.uploadFile).mockImplementation(() => {
    let resolve!: () => void;
    let reject!: (error: Error) => void;
    const done = new Promise<void>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    settles.push({ resolve, reject });
    return { done, abort: vi.fn() };
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

function queue(): UploadQueue {
  return new UploadQueue({ redraw: vi.fn(), onUploaded: vi.fn() });
}

const file = (name: string): { file: File; relativePath: string } => ({
  file: new File(["x"], name),
  relativePath: name,
});

describe("the upload panel's lifetime", () => {
  it("goes away a moment after everything has uploaded", async () => {
    const uploads = queue();
    uploads.add("/home/", [file("a.txt"), file("b.txt")]);
    settles[0].resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(uploads.items).toHaveLength(2);

    settles[1].resolve();
    await vi.advanceTimersByTimeAsync(FINISHED_LINGER_MS - 1);
    expect(uploads.items).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(uploads.items).toEqual([]);
  });

  it("stays up when an upload failed, and when more arrive before it goes", async () => {
    const failing = queue();
    failing.add("/home/", [file("a.txt")]);
    settles[0].reject(new Error("Disk full"));
    await vi.advanceTimersByTimeAsync(FINISHED_LINGER_MS * 2);
    expect(failing.items.map((item) => item.status)).toEqual(["failed"]);

    const busy = queue();
    busy.add("/home/", [file("b.txt")]);
    settles[1].resolve();
    await vi.advanceTimersByTimeAsync(FINISHED_LINGER_MS / 2);
    busy.add("/home/", [file("c.txt")]);
    await vi.advanceTimersByTimeAsync(FINISHED_LINGER_MS);
    expect(busy.items.map((item) => item.name)).toEqual(["b.txt", "c.txt"]);
  });
});

describe("a failed upload's row", () => {
  it("says why each failed from the refusal's kind, with the sentence and details behind it", async () => {
    const uploads = queue();
    uploads.add("/home/", [file("a.txt"), file("b.txt"), file("c.txt"), file("d.txt"), file("e.txt")]);
    settles[0].reject(httpFailure(0, null));
    const readOnly = "Your computer only shares this to read.";
    settles[1].reject(httpFailure(403, { detail: readOnly, user_facing_message: readOnly, kind: "shared_read_only" }));
    const full = "This workspace is out of disk space.";
    settles[2].reject(
      httpFailure(507, { detail: "PUT /home/c.txt", user_facing_message: full, kind: "out_of_space" }),
    );
    // Three go up at once, so the last two start once these have settled.
    await vi.advanceTimersByTimeAsync(0);
    settles[3].reject(httpFailure(403, { detail: "forbidden" }));
    settles[4].reject(httpFailure(500, { detail: "PUT /home/e.txt: [Errno 5] Input/output error" }));
    await vi.advanceTimersByTimeAsync(0);

    expect(uploads.items.map((item) => [item.status, item.failure?.line])).toEqual([
      ["failed", "Interrupted"],
      ["failed", "Failed — read-only folder"],
      ["failed", "Failed — out of disk space"],
      ["failed", "Failed"],
      ["failed", "Failed"],
    ]);
    expect(uploads.items[1].failure?.explanation).toBe(`"b.txt" couldn't be uploaded. ${readOnly}\nHTTP 403`);
    expect(uploads.items[4].failure?.explanation).toBe(
      '"e.txt" couldn\'t be uploaded. Try again.\nHTTP 500\nPUT /home/e.txt: [Errno 5] Input/output error',
    );
  });

  it("is canceled, not failed, when the upload was stopped on purpose", async () => {
    const uploads = queue();
    uploads.add("/home/", [file("a.txt")]);
    settles[0].reject(new DOMException("Canceled", "AbortError"));
    await vi.advanceTimersByTimeAsync(0);

    expect(uploads.items.map((item) => [item.status, item.failure])).toEqual([["canceled", null]]);
  });
});
