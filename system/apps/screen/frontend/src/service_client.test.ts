// @vitest-environment jsdom
/** What a refused service answer says about itself, and how a refused sign-in reaches the viewer. */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ServiceRequestError,
  describeRefusal,
  postCancelPendingUrl,
  postSession,
  postTakeControl,
} from "./service_client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("describeRefusal", () => {
  it("prefers the body's detail, else the status line", async () => {
    expect(await describeRefusal(jsonResponse(503, { detail: "the Screen is not running yet" }))).toBe(
      "the Screen is not running yet",
    );
    expect(await describeRefusal(new Response("not json", { status: 502, statusText: "Bad Gateway" }))).toBe(
      "502 Bad Gateway",
    );
    expect(await describeRefusal(new Response("", { status: 404 }))).toBe("status 404");
  });
});

describe("postSession", () => {
  it("throws the refusal's status and detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse(502, { detail: "neko refused the login: bad password" }))),
    );
    const error = await postSession().catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(ServiceRequestError);
    expect((error as ServiceRequestError).status).toBe(502);
    expect((error as ServiceRequestError).detail).toBe("neko refused the login: bad password");
  });

  it("answers the session when the service agrees", async () => {
    const session = { session_id: "s1", token: "tok", name: "josh", is_owner: true };
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse(200, session))),
    );
    expect(await postSession()).toEqual(session);
  });
});

describe("postCancelPendingUrl", () => {
  it("names the browser in the path and this session in the body", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init: RequestInit) => {
        calls.push({ url, init });
        return Promise.resolve(jsonResponse(200, { cancelled: true }));
      }),
    );
    await postCancelPendingUrl("browser-1", "s2");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/browsers/browser-1/pending-url/cancel");
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ session_id: "s2" });
  });
});

describe("postTakeControl", () => {
  it("answers whether the verb was accepted and why not", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse(404, { detail: "no such session" }))),
    );
    expect(await postTakeControl("s1")).toEqual({ ok: false, detail: "no such session" });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse(200, { ok: true }))),
    );
    expect(await postTakeControl("s1")).toEqual({ ok: true, detail: null });
  });
});
