import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the fetched URLs the bare /api paths.
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

// m.redraw needs a real render loop; stub it so the cache logic runs without a DOM.
const { mockRedraw } = vi.hoisted(() => ({ mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({
  default: { redraw: mockRedraw },
}));

import {
  enableCloudConnections,
  getCloudConnectionsOffer,
  isCloudConnectionsOfferTaken,
  resetCloudConnectionsOffersForTesting,
} from "./cloud-connections-offer";

const OFFERED = { is_offered: true, provider_name: "Anthropic" };

function jsonResponse(status: number, body: unknown): { ok: boolean; status: number; json: () => Promise<unknown> } {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

/** Let the fire-and-forget fetch chain inside getCloudConnectionsOffer settle. */
async function flushFetches(): Promise<void> {
  for (let hop = 0; hop < 6; hop += 1) {
    await Promise.resolve();
  }
}

beforeEach(() => {
  resetCloudConnectionsOffersForTesting();
  mockRedraw.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getCloudConnectionsOffer", () => {
  it("asks once per chat and scope, and redraws when the offer lands", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, OFFERED));
    vi.stubGlobal("fetch", fetchMock);

    expect(getCloudConnectionsOffer("chat-1", "google-gmail-api")).toBeNull();
    expect(getCloudConnectionsOffer("chat-1", "google-gmail-api")).toBeNull();
    await flushFetches();

    expect(getCloudConnectionsOffer("chat-1", "google-gmail-api")).toEqual(OFFERED);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/chats/chat-1/cloud-connections?scope=google-gmail-api");
    expect(mockRedraw).toHaveBeenCalled();
    // Another chat's card for the same scope is its own question.
    expect(getCloudConnectionsOffer("chat-2", "google-gmail-api")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("makes no offer when the backend makes none, errors, or cannot be reached", async () => {
    const answers = [
      async () => jsonResponse(200, { is_offered: false, provider_name: null }),
      async () => jsonResponse(404, { detail: "Chat not found" }),
      async () => {
        throw new Error("offline");
      },
    ];
    const fetchMock = vi.fn(() => (answers.shift() as () => Promise<unknown>)());
    vi.stubGlobal("fetch", fetchMock);

    for (const scope of ["slack-api", "notion-mcp-api", "github-rest-api"]) {
      expect(getCloudConnectionsOffer("chat-1", scope)).toBeNull();
      await flushFetches();
      expect(getCloudConnectionsOffer("chat-1", scope)).toBeNull();
    }
    // A hint is not worth asking for twice.
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

describe("enableCloudConnections", () => {
  it("posts the request id, marks the request taken, and drops the chat's other offers", async () => {
    const fetchMock = vi.fn(async (_path: string, init?: { method?: string }) =>
      init?.method === "POST"
        ? jsonResponse(200, { is_request_withdrawn: true, is_notice_delivered: true })
        : jsonResponse(200, OFFERED),
    );
    vi.stubGlobal("fetch", fetchMock);
    getCloudConnectionsOffer("chat-1", "slack-api");
    getCloudConnectionsOffer("chat-2", "slack-api");
    await flushFetches();

    const outcome = await enableCloudConnections("chat-1", "req-7");

    expect(outcome).toEqual({ ok: true, detail: "" });
    expect(fetchMock).toHaveBeenLastCalledWith("/api/chats/chat-1/cloud-connections/enable", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: "req-7" }),
    });
    expect(isCloudConnectionsOfferTaken("req-7")).toBe(true);
    expect(getCloudConnectionsOffer("chat-1", "slack-api")).toBeNull();
    expect(getCloudConnectionsOffer("chat-2", "slack-api")).toEqual(OFFERED);
  });

  it("reports the backend's own reason for a refusal and leaves the request untaken", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(500, { detail: "Failed to restart agent 'chat-1': it would not start" })),
    );

    const outcome = await enableCloudConnections("chat-1", "req-7");

    expect(outcome).toEqual({ ok: false, detail: "Failed to restart agent 'chat-1': it would not start" });
    expect(isCloudConnectionsOfferTaken("req-7")).toBe(false);
  });

  it("says so when the chat app cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("offline");
      }),
    );
    expect(await enableCloudConnections("chat-1", "req-7")).toEqual({
      ok: false,
      detail: "The chat app could not be reached.",
    });
  });
});
