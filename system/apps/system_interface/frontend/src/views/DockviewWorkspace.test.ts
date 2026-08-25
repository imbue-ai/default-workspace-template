import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, so provide
// a polyfill before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import {
  displayNameForView,
  equalTabWidth,
  isTitleTruncated,
  memberRefForPanelParams,
  refForShortcutFocus,
} from "./DockviewWorkspace";
import type { ProjectInfo } from "../models/Projects";
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
  it("finds nothing to focus in a view with no chat, so the shortcut creates", () => {
    const refs = ["terminal:work", "service:web", "service:browser?session=1"];
    expect(refForShortcutFocus(refs, "chat", {})).toBeNull();
  });

  it("goes to the one chat a view lists", () => {
    expect(refForShortcutFocus(["service:web", "chat:agent-a"], "chat", {})).toBe("chat:agent-a");
  });

  it("goes to the most recently used chat among several", () => {
    const refs = ["chat:agent-a", "chat:agent-b", "chat:agent-c"];
    const recency = { "chat:agent-b": 2_000, "chat:agent-a": 1_000 };
    expect(refForShortcutFocus(refs, "chat", recency)).toBe("chat:agent-b");
  });

  it("ranks members with no recency data behind any that have some", () => {
    const refs = ["chat:agent-a", "chat:agent-b"];
    expect(refForShortcutFocus(refs, "chat", { "chat:agent-b": 5 })).toBe("chat:agent-b");
  });

  it("takes the first listed when nothing has recency data", () => {
    expect(refForShortcutFocus(["chat:agent-a", "chat:agent-b"], "chat", {})).toBe("chat:agent-a");
  });

  it("ignores recency entries of other kinds", () => {
    // A terminal used moments ago must not steal the chat shortcut's focus.
    const refs = ["chat:agent-a", "terminal:work"];
    const recency = { "terminal:work": 9_000, "chat:agent-a": 1 };
    expect(refForShortcutFocus(refs, "chat", recency)).toBe("chat:agent-a");
  });

  it("finds nothing to focus in a view with no browser", () => {
    expect(refForShortcutFocus(["chat:agent-a", "terminal:work"], "browser", {})).toBeNull();
  });

  it("goes to the most recently used browser a view lists", () => {
    const refs = ["service:browser?session=2", "service:browser?session=5"];
    const recency = { "service:browser?session=5": 10 };
    expect(refForShortcutFocus(refs, "browser", recency)).toBe("service:browser?session=5");
  });

  it("keeps browsers apart from the apps and terminals sharing their ref scheme", () => {
    const refs = ["service:web", "service:terminal", "service:browser?session=1"];
    expect(refForShortcutFocus(refs, "browser", {})).toBe("service:browser?session=1");
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

  it("files an app pane under the instance it shows", () => {
    const params: PanelParams = {
      panelType: "iframe",
      agentId: "agent-primary",
      serviceName: "web",
      serviceInstanceId: "web-2",
    };
    expect(memberRefForPanelParams(params)).toBe("service:web?instance=web-2");
  });

  it("files nothing for an app pane whose instance has not landed yet", () => {
    // Mid-mint (or mid-adoption of a pre-instances layout) the pane is not an
    // object yet, exactly as a terminal before its session name is allocated.
    const params: PanelParams = { panelType: "iframe", agentId: "agent-primary", serviceName: "web" };
    expect(memberRefForPanelParams(params)).toBeNull();
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

describe("displayNameForView", () => {
  const PROJECTS: ProjectInfo[] = [
    { project_id: "project-1", name: "Alpha", color: "#3B82F6", glyph: 0, has_content: true, members: [] },
  ];

  it("names a project in the registry", () => {
    expect(displayNameForView("project-1", PROJECTS)).toBe("Alpha");
  });

  it("names Everything without looking it up -- it has no registry entry to find", () => {
    // The fallback a delete reports once the last project goes, so the one
    // message announcing zero projects must not read `switched to "everything"`.
    expect(displayNameForView("everything", PROJECTS)).toBe("Everything");
    expect(displayNameForView("everything", [])).toBe("Everything");
  });

  it("falls back to the bare id for a project the registry no longer holds", () => {
    expect(displayNameForView("gone", PROJECTS)).toBe("gone");
  });
});
