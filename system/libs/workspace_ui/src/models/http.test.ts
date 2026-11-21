import { afterEach, describe, expect, it, vi } from "vitest";
import { HttpError, getJson, postJson, requestJson } from "./http";

function answering(status: number, body: unknown, contentType = "application/json"): void {
  const text = body === undefined ? null : typeof body === "string" ? body : JSON.stringify(body);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(text, { status, headers: { "Content-Type": contentType } })),
  );
}

async function failureOf(request: Promise<unknown>): Promise<HttpError> {
  const failure = await request.catch((error: unknown) => error);
  expect(failure).toBeInstanceOf(HttpError);
  return failure as HttpError;
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

  it("reads a sentence the server wrote for people as such, its developer detail behind the details", async () => {
    answering(502, {
      detail: "chat answered the launch with 500: boom",
      user_facing_message: "Chat couldn't read its agent list. If this persists, report a bug.",
      reportable: true,
      kind: "launch_failed",
    });
    const { failure } = await failureOf(postJson("/api/x", {}));
    expect(failure).toEqual({
      cause: "user_facing",
      userFacingMessage: "Chat couldn't read its agent list. If this persists, report a bug.",
      reason: "chat answered the launch with 500: boom",
      isReportable: true,
      requestKind: "change",
      status: 502,
      details: "HTTP 502\nchat answered the launch with 500: boom",
      kind: "launch_failed",
      body: expect.objectContaining({ kind: "launch_failed" }),
    });
  });

  it("reads the server's detail as its reason, with the status a caller can tell refusals apart by", async () => {
    answering(409, { detail: "the stored layout is newer" });
    const error = await failureOf(postJson("/api/x", {}));
    expect(error.status).toBe(409);
    // The message stays the server's reason, for the screens not yet worded with failureSentence.
    expect(error.message).toBe("the stored layout is newer");
    expect(error.failure).toMatchObject({ cause: "reason", reason: "the stored layout is newer" });
    expect(error.failure.details).toBe("HTTP 409\nthe stored layout is newer");
  });

  it("reads a gateway status that carries a JSON reason as the server's own answer, not as no answer", async () => {
    answering(503, { detail: "Agent 'x' is not ready to receive messages yet" });
    expect((await failureOf(postJson("/api/x", {}))).failure.cause).toBe("reason");
  });

  it("reads a proxy's plain-text gateway status, and a dropped connection, as no answer", async () => {
    answering(504, "Backend timed out", "text/plain");
    const gateway = await failureOf(postJson("/api/x", {}));
    expect(gateway.failure).toMatchObject({ cause: "not_answered", details: "HTTP 504\nBackend timed out" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const dropped = await failureOf(postJson("/api/x", {}));
    expect(dropped.status).toBe(0);
    expect(dropped.failure).toMatchObject({ cause: "not_answered", details: "Failed to fetch" });
  });

  it("reads a refusal with no reason as such, the body behind the details", async () => {
    answering(500, "<html>Internal Server Error</html>", "text/html");
    const error = await failureOf(postJson("/api/x", {}));
    expect(error.failure).toMatchObject({
      cause: "no_reason",
      details: "HTTP 500\n<html>Internal Server Error</html>",
      body: null,
    });
  });
});

describe("getJson", () => {
  it("knows a failed GET only read", async () => {
    answering(503, undefined);
    expect((await failureOf(getJson("/api/x"))).failure).toMatchObject({
      cause: "not_answered",
      requestKind: "read",
    });
  });
});

describe("requestJson", () => {
  it("sends the method with a body only when one is given, and never from the cache", async () => {
    answering(200, { rows: [] });
    expect(await requestJson<{ rows: [] }>("GET", "/api/x")).toEqual({ rows: [] });
    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(init).toMatchObject({ method: "GET", cache: "no-store" });
    expect(init.body).toBeUndefined();
    expect(init.headers).toBeUndefined();

    answering(204, undefined);
    expect(await requestJson<void>("DELETE", "/api/x")).toBeUndefined();
    answering(200, { ok: true });
    await requestJson("PUT", "/api/x", { a: 1 });
    const [, withBody] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(withBody).toMatchObject({ method: "PUT", body: JSON.stringify({ a: 1 }) });
  });
});
