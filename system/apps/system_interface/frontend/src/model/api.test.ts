// @vitest-environment jsdom
/**
 * The widget routes as the frontend calls them, over a stubbed ``fetch``: what each one posts where, and the
 * record it answers.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { setWidgetEnabled, setWidgetPresentation } from "./api";

interface RecordedRequest {
  url: string;
  method: string;
  body: unknown;
}

/** Stub ``fetch`` to answer every request with ``body``, recording what was sent. */
function stubFetch(status: number, body: unknown): RecordedRequest[] {
  const requests: RecordedRequest[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({ url, method: String(init?.method), body: JSON.parse(String(init?.body)) });
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    }),
  );
  return requests;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the widget routes", () => {
  it("writes how this client shows a widget to its own route, and answers the client record", async () => {
    const presentation = { position: { x: 1, y: 0.5 }, mode: "collapsed", placement: "tray" } as const;
    const requests = stubFetch(200, {
      id: "client 1",
      active_desktop: "home",
      last_seen: "2026-09-19T00:00:00Z",
      widgets: { "my gauge": presentation },
    });
    const client = await setWidgetPresentation("client 1", "my gauge", presentation);
    expect(requests).toEqual([
      { url: "/api/clients/client%201/widgets/my%20gauge", method: "POST", body: presentation },
    ]);
    expect(client.widgets).toEqual({ "my gauge": presentation });
  });

  it("shows or hides a widget for the workspace, and answers every widget's switch", async () => {
    const requests = stubFetch(200, { widgets: [{ app: "gauge", is_enabled: false }] });
    expect(await setWidgetEnabled("gauge", false)).toEqual([{ app: "gauge", is_enabled: false }]);
    expect(requests).toEqual([{ url: "/api/widgets/gauge", method: "POST", body: { is_enabled: false } }]);
    // An answer naming no switches is none.
    stubFetch(200, {});
    expect(await setWidgetEnabled("gauge", true)).toEqual([]);
  });

  it("throws the shell's own detail when it refuses", async () => {
    stubFetch(404, { detail: "No app 'gauge' declares a widget" });
    await expect(setWidgetEnabled("gauge", true)).rejects.toThrow("No app 'gauge' declares a widget");
  });
});
