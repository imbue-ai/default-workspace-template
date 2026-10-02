// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { historyView, summary as summaryRecord } from "../testing/records";

type FetchReply = { readonly status: number; readonly body: unknown };

/** Stub ``fetch`` with replies the test releases one at a time, in whatever order it likes. */
function stubFetch(): { urls: string[]; reply: (index: number, reply: FetchReply) => void } {
  const urls: string[] = [];
  const resolvers: ((response: Response) => void)[] = [];
  vi.stubGlobal(
    "fetch",
    (url: string) =>
      new Promise<Response>((resolve) => {
        urls.push(url);
        resolvers.push(resolve);
      }),
  );
  return {
    urls,
    reply: (index, { status, body }) =>
      resolvers[index](
        new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
      ),
  };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => vi.resetModules());
afterEach(() => vi.unstubAllGlobals());

describe("the history model", () => {
  it("drops a slow reply for a range the user has already left", async () => {
    const fetches = stubFetch();
    const history = await import("./history");
    history.selectRange("HOUR");
    history.selectRange("WEEK");
    expect(fetches.urls).toEqual(["/api/history?range=hour", "/api/history?range=week"]);
    fetches.reply(1, { status: 200, body: historyView({ range: "WEEK" }) });
    await settle();
    fetches.reply(0, { status: 200, body: historyView({ range: "HOUR" }) });
    await settle();
    const state = history.getHistoryState();
    expect(state.kind === "loaded" && state.view.range).toBe("WEEK");
  });
});

describe("the summary model", () => {
  it("stays forbidden, and stops reading, once the page answers 403", async () => {
    vi.useFakeTimers();
    try {
      const fetches = stubFetch();
      const summary = await import("./summary");
      summary.startRefreshing();
      fetches.reply(0, { status: 403, body: {} });
      await vi.advanceTimersByTimeAsync(3 * summary.REFRESH_INTERVAL_MS);
      expect(summary.getSummaryState().kind).toBe("forbidden");
      expect(fetches.urls).toHaveLength(1);
      summary.stopRefreshing();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the newest reading when an older read answers after it", async () => {
    const fetches = stubFetch();
    const summary = await import("./summary");
    void summary.refreshNow();
    void summary.refreshNow();
    fetches.reply(1, { status: 200, body: summaryRecord("TIGHT", [], null) });
    await settle();
    fetches.reply(0, { status: 200, body: summaryRecord("COMFORTABLE", [], null) });
    await settle();
    const state = summary.getSummaryState();
    expect(state.kind === "loaded" && state.summary.memory?.status).toBe("TIGHT");
  });

  it("says a read that took too long, rather than the browser's error name", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new DOMException("signal timed out", "TimeoutError")));
    const summary = await import("./summary");
    await summary.refreshNow();
    const state = summary.getSummaryState();
    expect(state.kind === "failed" && state.message).toBe("It took too long to answer.");
  });

  it("measures idle times on the workspace's clock, not the browser's", async () => {
    const fetches = stubFetch();
    const summary = await import("./summary");
    const workspaceNow = Date.now() + 15 * 60 * 1000;
    const read = summary.refreshNow();
    fetches.reply(0, {
      status: 200,
      body: { ...summaryRecord("COMFORTABLE", [], null), measured_at: new Date(workspaceNow).toISOString() },
    });
    await read;
    expect(Math.abs(summary.serverNowMs() - workspaceNow)).toBeLessThan(1000);
  });

  it("tells a stop refused because the chat started working apart from any other refusal", async () => {
    const fetches = stubFetch();
    const summary = await import("./summary");
    const working = summary.requestChatAction("c1", "stop", false);
    fetches.reply(0, { status: 409, body: { detail: "busy", chat_status: "working" } });
    expect(await working).toEqual({ kind: "started_working" });
    const other = summary.requestChatAction("c1", "stop", false);
    fetches.reply(1, { status: 409, body: { detail: "already stopping" } });
    expect(await other).toEqual({ kind: "failed", message: "already stopping" });
    const done = summary.requestChatAction("c 1", "start", false);
    fetches.reply(2, { status: 200, body: { status: "ok" } });
    expect(await done).toEqual({ kind: "done" });
    expect(fetches.urls[2]).toBe("/api/chats/c%201/start");
  });
});

describe("the storage model", () => {
  it("keeps the last measurement when another window is already measuring", async () => {
    const fetches = stubFetch();
    const storage = await import("./storage");
    const first = storage.measureStorage();
    fetches.reply(0, {
      status: 200,
      body: {
        measured_at: "2026-10-01T12:00:00Z",
        total_kib: 5,
        categories: [],
        largest: [],
        command: "du",
        notes: [],
        measure_seconds: 1,
      },
    });
    await first;
    const second = storage.measureStorage();
    fetches.reply(1, { status: 429, body: { detail: "already measuring" } });
    await second;
    const state = storage.getStorageState();
    expect(state.kind === "loaded" && state.summary.total_kib).toBe(5);
  });
});
