// @vitest-environment jsdom
/** A dropped file goes to the service's drop route as the controlling session, and the strip says where it landed. */
import { afterEach, describe, expect, it, vi } from "vitest";

import { DROP_MAX_BYTES, describeDropOutcome, uploadDroppedFiles } from "./files";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("uploadDroppedFiles", () => {
  it("posts the files with the session id as multipart and answers where they landed", async () => {
    const calls: { url: string; body: FormData }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init: RequestInit) => {
        calls.push({ url, body: init.body as FormData });
        return Promise.resolve(jsonResponse(200, { directory: "/home/user/Desktop", saved: ["notes.txt"] }));
      }),
    );
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    const outcome = await uploadDroppedFiles([file], "s1");
    expect(outcome).toEqual({ landed: { directory: "/home/user/Desktop", saved: ["notes.txt"] } });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/drop");
    expect(calls[0].body.get("session_id")).toBe("s1");
    const sent = calls[0].body.getAll("files");
    expect(sent).toHaveLength(1);
    expect((sent[0] as File).name).toBe("notes.txt");
  });

  it("answers the service's refusal and never posts an oversize drop", async () => {
    const fetchSpy = vi.fn(() =>
      Promise.resolve(jsonResponse(403, { detail: "only the session in control of the Screen may do this" })),
    );
    vi.stubGlobal("fetch", fetchSpy);
    const small = new File(["x"], "a.txt");
    expect(await uploadDroppedFiles([small], "s1")).toEqual({
      refused: "only the session in control of the Screen may do this",
    });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const huge = { name: "big.bin", size: DROP_MAX_BYTES + 1 } as File;
    expect(await uploadDroppedFiles([huge], "s1")).toEqual({ refused: "the files are over the 64 MiB cap" });
    expect(await uploadDroppedFiles([], "s1")).toEqual({ refused: "nothing to drop" });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
});

describe("describeDropOutcome", () => {
  it("names the folder and the file, counts several, and says why a drop failed", () => {
    expect(describeDropOutcome({ landed: { directory: "/home/user/Desktop", saved: ["report-1.pdf"] } })).toBe(
      "Saved to /home/user/Desktop: report-1.pdf",
    );
    expect(describeDropOutcome({ landed: { directory: "/home/user/Desktop", saved: ["a.txt", "b.txt"] } })).toBe(
      "Saved 2 files to /home/user/Desktop",
    );
    expect(describeDropOutcome({ refused: "the Screen is not running" })).toBe(
      "The drop failed: the Screen is not running",
    );
  });
});
