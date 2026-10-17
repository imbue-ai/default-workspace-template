import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as dufs from "./dufs";
import { FINISHED_LINGER_MS, UploadQueue } from "./uploads";

vi.mock("./dufs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./dufs")>()),
  uploadFile: vi.fn(),
}));

/** Each upload's settle functions, in the order they started. */
let settles: { resolve: () => void; reject: (error: Error) => void }[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  settles = [];
  vi.mocked(dufs.uploadFile).mockImplementation(() => {
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
