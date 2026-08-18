import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, so provide
// a polyfill before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import {
  equalTabWidth,
  isTitleTruncated,
  memberRefForPanelParams,
  preferredChatRefForView,
  refForShortcutFocus,
} from "./DockviewWorkspace";
import type { PanelParams } from "./liveSurfaces";

describe("equalTabWidth", () => {
  it("shares what is left of a strip once the '+' is accounted for", () => {
    // (644 - 44) / 4 = 150.
    expect(equalTabWidth([{ width: 644, tabCount: 4 }])).toBe(150);
  });

  it("takes the narrowest strip's ideal so no strip has to scroll", () => {
    // The 4-tab strip wants 150, the 6-tab one wants 144; everybody gets 144.
    expect(
      equalTabWidth([
        { width: 644, tabCount: 4 },
        { width: 908, tabCount: 6 },
      ]),
    ).toBe(144);
  });

  it("clamps a crowded strip up to the floor", () => {
    // (400 - 44) / 12 = 29.7, far below anything readable.
    expect(equalTabWidth([{ width: 400, tabCount: 12 }])).toBe(140);
  });

  it("clamps a roomy strip down to the 220px ceiling", () => {
    expect(equalTabWidth([{ width: 1200, tabCount: 1 }])).toBe(220);
  });

  it("ignores strips that hold no tabs", () => {
    // An empty strip's ideal would be infinite and would never win anyway; a
    // strip mid-teardown must not drag the shared width to the ceiling either.
    expect(
      equalTabWidth([
        { width: 644, tabCount: 4 },
        { width: 300, tabCount: 0 },
      ]),
    ).toBe(150);
  });

  it("answers with the ceiling when there are no tabs at all", () => {
    expect(equalTabWidth([{ width: 300, tabCount: 0 }])).toBe(220);
    expect(equalTabWidth([])).toBe(220);
  });

  it("rounds to whole pixels", () => {
    // (500 - 44) / 7 = 65.14 -> clamped to the floor; (700 - 44) / 5 = 131.2.
    expect(equalTabWidth([{ width: 700, tabCount: 5 }])).toBe(140);
  });

  it("never goes negative on a strip narrower than its own reserved space", () => {
    expect(equalTabWidth([{ width: 20, tabCount: 1 }])).toBe(140);
  });
});

describe("isTitleTruncated", () => {
  it("is false for a title that fits", () => {
    expect(isTitleTruncated(80, 140)).toBe(false);
  });

  it("is true for a title wider than its box", () => {
    expect(isTitleTruncated(260, 140)).toBe(true);
  });

  it("tolerates a sub-pixel overhang so an exact fit stays crisp", () => {
    expect(isTitleTruncated(140.4, 140)).toBe(false);
    expect(isTitleTruncated(141.5, 140)).toBe(true);
  });
});

describe("refForShortcutFocus", () => {
  const PRIMARY_CHAT = "chat:agent-primary";

  it("finds nothing to focus in a view with no chat, so the shortcut creates", () => {
    const refs = ["terminal:work", "service:web", "service:browser?session=1"];
    expect(refForShortcutFocus(refs, "chat", PRIMARY_CHAT)).toBeNull();
  });

  it("goes to the one chat a view lists", () => {
    expect(refForShortcutFocus(["service:web", "chat:agent-a"], "chat", null)).toBe("chat:agent-a");
  });

  it("prefers the named chat over the others", () => {
    const refs = ["chat:agent-a", PRIMARY_CHAT, "chat:agent-b"];
    expect(refForShortcutFocus(refs, "chat", PRIMARY_CHAT)).toBe(PRIMARY_CHAT);
  });

  it("falls back to the first chat the view lists when the named one is not among them", () => {
    const refs = ["chat:agent-a", "chat:agent-b"];
    expect(refForShortcutFocus(refs, "chat", PRIMARY_CHAT)).toBe("chat:agent-a");
  });

  it("takes the first chat when the view names none", () => {
    expect(refForShortcutFocus(["chat:agent-a", "chat:agent-b"], "chat", null)).toBe("chat:agent-a");
  });

  it("ignores a preferred ref of another kind", () => {
    // A view's own chat can never be a browser, but the two shortcuts share
    // this function and a mismatch must not leak across them.
    const refs = ["chat:agent-a", "service:browser?session=2"];
    expect(refForShortcutFocus(refs, "browser", "chat:agent-a")).toBe("service:browser?session=2");
  });

  it("finds nothing to focus in a view with no browser", () => {
    expect(refForShortcutFocus(["chat:agent-a", "terminal:work"], "browser", null)).toBeNull();
  });

  it("goes to the one browser a view lists", () => {
    const refs = ["chat:agent-a", "service:browser?session=2"];
    expect(refForShortcutFocus(refs, "browser", null)).toBe("service:browser?session=2");
  });

  it("takes the first browser when a view lists several", () => {
    // A browser has no per-view singleton, so listing order decides.
    const refs = ["service:browser?session=2", "service:browser?session=5"];
    expect(refForShortcutFocus(refs, "browser", null)).toBe("service:browser?session=2");
  });

  it("keeps browsers apart from the apps and terminals sharing their ref scheme", () => {
    const refs = ["service:web", "service:terminal", "service:browser?session=1"];
    expect(refForShortcutFocus(refs, "browser", null)).toBe("service:browser?session=1");
  });
});

describe("preferredChatRefForView", () => {
  const PRIMARY = "agent-primary";
  // Which project each chat agent was started in, keyed by the chat's ref.
  const ORIGINS = { "chat:agent-own": "project-1", "chat:agent-other": "project-2" };

  it("names the chat a project was made with, whatever else the project holds", () => {
    const refs = ["chat:agent-other", "service:web", "chat:agent-own"];
    expect(preferredChatRefForView(refs, "project-1", ORIGINS, PRIMARY)).toBe("chat:agent-own");
  });

  it("keeps naming it once the project has been given other chats", () => {
    // Membership is many-to-many, so a project may show chats started
    // elsewhere -- including the primary agent's. The shortcut is still a
    // singleton pointing at the project's own chat.
    const refs = [`chat:${PRIMARY}`, "chat:agent-other", "chat:agent-own"];
    expect(preferredChatRefForView(refs, "project-1", ORIGINS, PRIMARY)).toBe("chat:agent-own");
  });

  it("names nothing when the project's own chat is not one it shows", () => {
    // Removed from the project, or destroyed: listing order decides again, and
    // a project showing no chat at all has the shortcut create one.
    expect(preferredChatRefForView(["chat:agent-other"], "project-1", ORIGINS, PRIMARY)).toBeNull();
    expect(preferredChatRefForView([], "project-1", ORIGINS, PRIMARY)).toBeNull();
  });

  it("names nothing while the project's chat is still starting up", () => {
    // A proto agent carries no label yet, so it is absent from the map.
    expect(preferredChatRefForView(["chat:agent-starting"], "project-1", ORIGINS, PRIMARY)).toBeNull();
  });

  it("names the primary agent's chat in Everything, which has no chat of its own", () => {
    const refs = ["chat:agent-own", `chat:${PRIMARY}`];
    expect(preferredChatRefForView(refs, "everything", ORIGINS, PRIMARY)).toBe(`chat:${PRIMARY}`);
  });

  it("names nothing in Everything when the primary agent id is unknown", () => {
    // The id comes off a meta tag, which an embedder may not have written.
    expect(preferredChatRefForView(["chat:agent-own"], "everything", ORIGINS, "")).toBeNull();
  });

  it("does not let a project inherit Everything's preference", () => {
    // The primary agent's chat is a member here but was started elsewhere, so
    // it is not this project's chat.
    const refs = [`chat:${PRIMARY}`];
    expect(preferredChatRefForView(refs, "project-1", ORIGINS, PRIMARY)).toBeNull();
  });
});

describe("memberRefForPanelParams", () => {
  it("files a chat under its stable agent id", () => {
    const params: PanelParams = { panelType: "chat", agentId: "agent-1", chatAgentId: "agent-1" };
    expect(memberRefForPanelParams(params)).toBe("chat:agent-1");
  });

  it("files a chat with no resolvable agent id nowhere", () => {
    expect(memberRefForPanelParams({ panelType: "chat", agentId: "" })).toBeNull();
  });

  it("files a persistent terminal under its tmux session name", () => {
    const params: PanelParams = {
      panelType: "iframe",
      agentId: "agent-primary",
      terminalSessionName: "terminal-2",
      terminalId: "term-abc",
    };
    expect(memberRefForPanelParams(params)).toBe("terminal:terminal-2");
  });

  it("files a registered app under its service name", () => {
    const params: PanelParams = { panelType: "iframe", agentId: "agent-primary", serviceName: "web" };
    expect(memberRefForPanelParams(params)).toBe("service:web");
  });

  it("files a browser fleet pane under its session, parsed off the url's query", () => {
    const params: PanelParams = {
      panelType: "iframe",
      agentId: "agent-primary",
      serviceName: "browser",
      url: "https://browser.example/?session=browser-2",
    };
    expect(memberRefForPanelParams(params)).toBe("service:browser?session=browser-2");
  });

  it("files a launcher nowhere -- it is a question about a pane, not an object", () => {
    expect(memberRefForPanelParams({ panelType: "launcher", agentId: "agent-primary" })).toBeNull();
  });

  it("files nothing when there are no params at all", () => {
    expect(memberRefForPanelParams(undefined)).toBeNull();
  });

  it("no longer files an ad-hoc URL page under a url:<hash> ref", () => {
    // Previously fell back to memberRef("url", await shortHash(panelId)): a
    // ref that named the PANEL rather than anything durable about the page.
    // A panel with none of a chat's agent id, a terminal's session name, or a
    // service name is not a persistent object the way the four real member
    // kinds are, so it now files nothing.
    const params: PanelParams = {
      panelType: "iframe",
      agentId: "agent-primary",
      url: "https://example.com/",
      title: "Example",
    };
    expect(memberRefForPanelParams(params)).toBeNull();
  });

  it("files nothing for a subagent view either, which sets none of the three identifying fields", () => {
    const params: PanelParams = { panelType: "subagent", agentId: "agent-primary", subagentSessionId: "sub-1" };
    expect(memberRefForPanelParams(params)).toBeNull();
  });

  it("does not yet file a terminal still allocating its tmux session name", () => {
    // terminalId is set synchronously at creation; terminalSessionName lands
    // once the backend hands back a free name (see addPanelForRef's
    // service:terminal branch, which re-files the panel once it does).
    const params: PanelParams = { panelType: "iframe", agentId: "agent-primary", terminalId: "term-abc", url: "" };
    expect(memberRefForPanelParams(params)).toBeNull();
  });
});
