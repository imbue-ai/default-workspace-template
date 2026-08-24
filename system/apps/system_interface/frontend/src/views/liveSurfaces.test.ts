import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, so provide
// a polyfill before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import { duplicateLiveKeyPanelIds, liveKeyForPanel, liveKeyForRef, sessionParamFromUrl } from "./liveSurfaces";
import type { PanelParams } from "./liveSurfaces";

describe("sessionParamFromUrl", () => {
  it("reads the session a browser pane addresses", () => {
    expect(sessionParamFromUrl("http://browser.example.com/?session=quiet-fox")).toBe("quiet-fox");
  });

  it("is null for a url that names no session", () => {
    expect(sessionParamFromUrl("http://web.example.com/")).toBeNull();
    expect(sessionParamFromUrl("http://web.example.com/?theme=dark")).toBeNull();
  });

  it("is null for an absent or empty url", () => {
    expect(sessionParamFromUrl(undefined)).toBeNull();
    expect(sessionParamFromUrl("")).toBeNull();
  });

  it("ignores a trailing fragment", () => {
    expect(sessionParamFromUrl("http://browser.example.com/?session=two#pane")).toBe("two");
  });

  it("reads a relative url, which has no origin to resolve against", () => {
    // The parse is deliberately string-level: this runs in contexts (and in
    // tests) where there is no ``location`` to resolve a relative address.
    expect(sessionParamFromUrl("/viewer?session=three")).toBe("three");
  });
});

/** A panel's params, with only the fields a case is about spelled out. */
function params(overrides: Partial<PanelParams>): PanelParams {
  return { panelType: "iframe", agentId: "primary", ...overrides };
}

describe("liveKeyForPanel", () => {
  it("files a chat under its agent id, so a rename does not fork the page", () => {
    expect(liveKeyForPanel("chat-a1", params({ panelType: "chat", agentId: "a1" }))).toBe("chat:a1");
  });

  it("prefers a chat's explicit chatAgentId over the owning agentId", () => {
    expect(liveKeyForPanel("chat-a1", params({ panelType: "chat", agentId: "owner", chatAgentId: "a1" }))).toBe(
      "chat:a1",
    );
  });

  it("files a terminal under its tmux session", () => {
    expect(liveKeyForPanel("terminal-session-x", params({ terminalSessionName: "terminal-2" }))).toBe(
      "terminal:terminal-2",
    );
  });

  it("falls back to the panel while a terminal is still allocating its session", () => {
    // The tmux name is allocated asynchronously, so the page exists before the
    // identity it will end up filed under does.
    expect(liveKeyForPanel("iframe-terminal-7", params({ terminalId: "term-7", url: "" }))).toBe(
      "panel:iframe-terminal-7",
    );
  });

  it("files an app pane under the instance it shows", () => {
    // The canonical instance name IS the object's identity: two instances of
    // one service are two objects with a live page each.
    const first = liveKeyForPanel(
      "app-instance-web-1",
      params({ serviceName: "web", url: "http://web.example.com", serviceInstanceId: "web-1" }),
    );
    const second = liveKeyForPanel(
      "app-instance-web-2",
      params({ serviceName: "web", url: "http://web.example.com", serviceInstanceId: "web-2" }),
    );
    expect(first).toBe("service:web?instance=web-1");
    expect(second).toBe("service:web?instance=web-2");
    expect(first).not.toBe(second);
  });

  it("falls back to the panel while an app pane's instance is still landing", () => {
    // Mid-mint (or mid-adoption of a pre-instances layout) the pane is not an
    // object yet, exactly as a terminal before its session name is allocated.
    expect(liveKeyForPanel("iframe-p-1", params({ serviceName: "web", url: "http://web.example.com" }))).toBe(
      "panel:iframe-p-1",
    );
  });

  it("files each fleet browser under its own session", () => {
    const one = liveKeyForPanel("iframe-p-1", params({ serviceName: "browser", url: "http://b/?session=one" }));
    const two = liveKeyForPanel("iframe-p-2", params({ serviceName: "browser", url: "http://b/?session=two" }));
    expect(one).toBe("service:browser?session=one");
    expect(two).toBe("service:browser?session=two");
    expect(one).not.toBe(two);
  });

  it("keeps a sessionless browser pane off the per-session keys", () => {
    expect(liveKeyForPanel("iframe-p-3", params({ serviceName: "browser", url: "http://b/" }))).toBe(
      "panel:iframe-p-3",
    );
  });

  it("files an ad-hoc page under its panel", () => {
    expect(liveKeyForPanel("iframe-p-9", params({ url: "https://example.com" }))).toBe("panel:iframe-p-9");
  });

  it("files a subagent view under its panel", () => {
    expect(liveKeyForPanel("subagent-a1-s2", params({ panelType: "subagent", subagentSessionId: "s2" }))).toBe(
      "panel:subagent-a1-s2",
    );
  });

  it("gives a launcher no key: it is a question about a pane, not an object", () => {
    expect(liveKeyForPanel("new-tab-1", params({ panelType: "launcher" }))).toBeNull();
  });

  it("gives an unidentifiable panel no key rather than guessing one", () => {
    expect(liveKeyForPanel("chat-", params({ panelType: "chat", agentId: "" }))).toBeNull();
    expect(liveKeyForPanel("iframe-p-1", undefined)).toBeNull();
  });
});

describe("liveKeyForRef", () => {
  it("passes through the refs that are already live keys", () => {
    expect(liveKeyForRef("chat:a1", "chat-a1")).toBe("chat:a1");
    expect(liveKeyForRef("terminal:terminal-2", "terminal-session-terminal-2")).toBe("terminal:terminal-2");
    expect(liveKeyForRef("service:browser?session=one", "iframe-p-2")).toBe("service:browser?session=one");
  });

  it("passes an instance ref through as its own live key", () => {
    expect(liveKeyForRef("service:web?instance=web-2", "app-instance-web-2")).toBe("service:web?instance=web-2");
  });

  it("maps a bare service ref onto the panel: it is a pin, not an object with a page", () => {
    expect(liveKeyForRef("service:web", "iframe-p-1")).toBe("panel:iframe-p-1");
  });

  it("maps an ad-hoc page's hashed ref back onto its panel", () => {
    expect(liveKeyForRef("url:deadbeef", "iframe-p-9")).toBe("panel:iframe-p-9");
  });

  it("agrees with liveKeyForPanel on every kind that has a durable identity", () => {
    const cases: { ref: string; panelId: string; params: PanelParams }[] = [
      { ref: "chat:a1", panelId: "chat-a1", params: params({ panelType: "chat", agentId: "a1" }) },
      {
        ref: "terminal:terminal-2",
        panelId: "terminal-session-terminal-2",
        params: params({ terminalSessionName: "terminal-2" }),
      },
      { ref: "service:web", panelId: "iframe-p-1", params: params({ serviceName: "web" }) },
      {
        ref: "service:browser?session=one",
        panelId: "iframe-p-2",
        params: params({ serviceName: "browser", url: "http://b/?session=one" }),
      },
    ];
    for (const entry of cases) {
      expect(liveKeyForRef(entry.ref, entry.panelId)).toBe(liveKeyForPanel(entry.panelId, entry.params));
    }
  });
});

describe("duplicateLiveKeyPanelIds", () => {
  it("finds nothing when every panel names a different object", () => {
    expect(
      duplicateLiveKeyPanelIds([
        { panelId: "chat-a1", key: "chat:a1" },
        { panelId: "iframe-p-1", key: "service:web" },
      ]),
    ).toEqual([]);
  });

  it("keeps the first occurrence and drops the rest", () => {
    expect(
      duplicateLiveKeyPanelIds([
        { panelId: "iframe-p-1", key: "service:web" },
        { panelId: "iframe-p-2", key: "service:web" },
        { panelId: "iframe-p-3", key: "service:web" },
      ]),
    ).toEqual(["iframe-p-2", "iframe-p-3"]);
  });

  it("never dedups panels that are not objects against each other", () => {
    // Two launchers in two panes are two legitimate questions, not one object
    // shown twice.
    expect(
      duplicateLiveKeyPanelIds([
        { panelId: "new-tab-1", key: null },
        { panelId: "new-tab-2", key: null },
      ]),
    ).toEqual([]);
  });
});
