import { afterEach, describe, expect, it, vi } from "vitest";
import { HttpError, postJson } from "./http";

function answering(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(body === undefined ? null : JSON.stringify(body), { status })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("postJson", () => {
  it("answers the parsed reply, and nothing for a 204", async () => {
    answering(200, { updated_at: "t1" });
    expect(await postJson<{ updated_at: string }>("/api/x", { a: 1 })).toEqual({ updated_at: "t1" });
    answering(204, undefined);
    expect(await postJson<void>("/api/x", {})).toBeUndefined();
  });

  it("throws the server's detail with the status a caller can tell refusals apart by", async () => {
    answering(409, { detail: "the stored layout is newer" });
    const failure = await postJson("/api/x", {}).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(HttpError);
    expect(failure).toMatchObject({ status: 409, message: "the stored layout is newer" });
  });
});
