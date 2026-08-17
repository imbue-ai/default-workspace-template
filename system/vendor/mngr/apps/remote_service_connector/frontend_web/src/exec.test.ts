// Grants CAS behavior of the ExecClient against a stubbed fetch: revision
// threading on read, base_revision on conditional writes, and the 409
// conflict surfacing as GrantsConflictError with the current document.

import { afterEach, describe, expect, it, vi } from "vitest";
import { generateKeypair } from "./crypto/ed25519";
import { ExecClient, GrantsConflictError } from "./exec";

const AUDIENCE = "host-abc.owner.us1.example.com";
const BASE_URL = "https://owner-exec-x1y2.host-abc.owner.us1.example.com";

async function makeClient(): Promise<ExecClient> {
  return new ExecClient(BASE_URL, AUDIENCE, await generateKeypair());
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ExecClient grants", () => {
  it("returns the document with its revision from getGrants", async () => {
    const seen: { url: string; method?: string }[] = [];
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      seen.push({ url, method: init?.method });
      return jsonResponse(200, {
        grants_toml: "[workspace]\n",
        revision: "r1",
      });
    });

    const doc = await (await makeClient()).getGrants();

    expect(doc).toEqual({ grantsToml: "[workspace]\n", revision: "r1" });
    expect(seen).toEqual([{ url: `${BASE_URL}/grants`, method: "GET" }]);
  });

  it("sends base_revision on putGrants and returns the new revision", async () => {
    let sentBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
      sentBody = JSON.parse(
        new TextDecoder().decode(init?.body as Uint8Array),
      ) as Record<string, unknown>;
      return jsonResponse(200, { written: true, revision: "r2" });
    });

    const result = await (await makeClient()).putGrants("[workspace]\n", "r1");

    expect(result).toEqual({ revision: "r2" });
    expect(sentBody).toEqual({
      grants_toml: "[workspace]\n",
      base_revision: "r1",
    });
  });

  it("omits base_revision when none is given (blind reset)", async () => {
    let sentBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
      sentBody = JSON.parse(
        new TextDecoder().decode(init?.body as Uint8Array),
      ) as Record<string, unknown>;
      return jsonResponse(200, { written: true, revision: "r1" });
    });

    await (await makeClient()).putGrants("[workspace]\n");

    expect(sentBody).toEqual({ grants_toml: "[workspace]\n" });
  });

  it("raises GrantsConflictError carrying the current document on 409", async () => {
    vi.stubGlobal("fetch", async () =>
      jsonResponse(409, {
        error: "stale base_revision",
        grants_toml: '[workspace]\nemails = ["other@example.com"]\n',
        revision: "r9",
      }),
    );

    const attempt = (await makeClient()).putGrants("[workspace]\n", "r1");

    await expect(attempt).rejects.toThrow(GrantsConflictError);
    await expect(attempt).rejects.toMatchObject({
      current: {
        grantsToml: '[workspace]\nemails = ["other@example.com"]\n',
        revision: "r9",
      },
    });
  });
});

describe("ExecClient writeFile", () => {
  it("serializes mode as an octal string (the daemon parses it base 8)", async () => {
    let sentBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
      sentBody = JSON.parse(
        new TextDecoder().decode(init?.body as Uint8Array),
      ) as Record<string, unknown>;
      return jsonResponse(200, {});
    });

    await (await makeClient()).writeFile("/etc/restic.env", "Zm9v", 0o600);

    expect(sentBody).toEqual({
      path: "/etc/restic.env",
      content_b64: "Zm9v",
      mode: "600",
    });
  });

  it("omits mode when none is given", async () => {
    let sentBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
      sentBody = JSON.parse(
        new TextDecoder().decode(init?.body as Uint8Array),
      ) as Record<string, unknown>;
      return jsonResponse(200, {});
    });

    await (await makeClient()).writeFile("/etc/restic.env", "Zm9v");

    expect(sentBody).toEqual({ path: "/etc/restic.env", content_b64: "Zm9v" });
  });
});
