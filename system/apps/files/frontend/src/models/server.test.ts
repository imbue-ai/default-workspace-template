// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError } from "@imbue/workspace-ui/src/models/http";

import { exists, listFolder, movePath } from "./server";

function answering(status: number, body: string, contentType: string): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(body, { status, headers: { "Content-Type": contentType } })),
  );
}

async function failureOf(request: Promise<unknown>): Promise<HttpError> {
  const error = await request.catch((caught: unknown) => caught);
  expect(error).toBeInstanceOf(HttpError);
  return error as HttpError;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a refused request to the Files server", () => {
  it("reads the server's sentence for people, a change, with its status behind the details", async () => {
    const said = "There's already something at /home/zeta/b.txt.";
    answering(412, JSON.stringify({ detail: said, user_facing_message: said }), "application/json");

    const { failure } = await failureOf(movePath("/home/b.txt", "/home/zeta/b.txt"));

    expect(failure).toMatchObject({
      cause: "user_facing",
      userFacingMessage: said,
      requestKind: "change",
      status: 412,
      details: `HTTP 412\n${said}`,
    });
  });

  it("reads a proxy's plain-text gateway status on a folder read as nothing having answered", async () => {
    answering(502, "Bad Gateway", "text/plain");

    const { failure } = await failureOf(listFolder("/home/"));

    expect(failure).toMatchObject({ cause: "not_answered", requestKind: "read", details: "HTTP 502\nBad Gateway" });
  });

  it("says a path that is a file is not a folder, the path kept behind the details", async () => {
    answering(200, JSON.stringify({ kind: "file", is_text: true }), "application/json");

    const { failure } = await failureOf(listFolder("/home/notes.txt/"));

    expect(failure).toMatchObject({
      cause: "user_facing",
      userFacingMessage: "It's a file, not a folder.",
      details: "HTTP 400\n/home/notes.txt/ is a file, not a folder.",
    });
  });

  it("reads a name check nothing answered as a read nothing answered", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))),
    );

    const { failure } = await failureOf(exists("/home/zeta/b.txt"));

    expect(failure).toMatchObject({ cause: "not_answered", requestKind: "read", details: "Failed to fetch" });
  });

  it("lists a folder that is not there as missing rather than failed", async () => {
    answering(404, JSON.stringify({ detail: "There's nothing at /gone/." }), "application/json");

    expect(await listFolder("/gone/")).toEqual({ exists: false, entries: [] });
  });
});
