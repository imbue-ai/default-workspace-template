// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { historyView } from "../testing/records";

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
    const fetches = stubFetch();
    const summary = await import("./summary");
    summary.startRefreshing();
    fetches.reply(0, { status: 403, body: {} });
    await settle();
    expect(summary.getSummaryState().kind).toBe("forbidden");
    summary.stopRefreshing();
    expect(fetches.urls).toHaveLength(1);
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
