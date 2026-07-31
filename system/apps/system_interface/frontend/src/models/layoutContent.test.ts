import { describe, expect, it } from "vitest";

import { layoutContentsAreEquivalent, type PanelParams, type SavedLayout } from "./layoutContent";

interface LeafOptions {
  /** Pixel size of the grid leaf, as the saving client's window happened to be. */
  size?: number;
  /** Which tab that client had selected in the group. */
  activeView?: string;
}

/**
 * Build a one-group layout holding ``panels``, in the shape dockview serializes.
 *
 * The dockview half is cast once here: its type comes from dockview-core, whose
 * enums are runtime values this (DOM-free) test does not want to import.
 */
function makeLayout(
  panels: Record<string, PanelParams>,
  options: LeafOptions & { gridWidth?: number; gridHeight?: number; activeGroup?: string } = {},
): SavedLayout {
  const panelIds = Object.keys(panels);
  const dockview = {
    grid: {
      root: {
        type: "branch",
        data: [
          {
            type: "leaf",
            data: { id: "group-1", views: panelIds, activeView: options.activeView ?? panelIds[0] },
            size: options.size ?? 500,
          },
        ],
        size: options.size ?? 500,
      },
      width: options.gridWidth ?? 1600,
      height: options.gridHeight ?? 900,
      orientation: "HORIZONTAL",
    },
    panels: Object.fromEntries(
      panelIds.map((id) => [
        id,
        { id, contentComponent: panels[id].panelType, title: panels[id].title, params: panels[id] },
      ]),
    ),
    activeGroup: options.activeGroup ?? "group-1",
  };
  return { dockview: dockview as unknown as SavedLayout["dockview"], panelParams: panels };
}

function chatPanel(agentId: string): PanelParams {
  return { panelType: "chat", agentId, chatAgentId: agentId, title: agentId };
}

function terminalPanel(sessionName: string, terminalId: string): PanelParams {
  return {
    panelType: "iframe",
    agentId: "primary",
    title: sessionName,
    terminalSessionName: sessionName,
    terminalId,
    url: `/service/terminal/?arg=_&arg=session&arg=${sessionName}&arg=${terminalId}&arg=`,
  };
}

// This is the definition that decides both whether an autosave fires and whether
// a remote layout is re-applied, so what it treats as "the same" is exactly what
// two clients are allowed to disagree about without fighting.
describe("layout content equivalence", () => {
  it("ignores the window geometry the layout happens to be displayed at", () => {
    const wide = makeLayout({ "chat-a": chatPanel("a") }, { gridWidth: 2560, gridHeight: 1400, size: 2100 });
    const narrow = makeLayout({ "chat-a": chatPanel("a") }, { gridWidth: 1280, gridHeight: 720, size: 900 });
    expect(layoutContentsAreEquivalent(wide, narrow)).toBe(true);
  });

  it("ignores which tab each client is looking at", () => {
    const panels = { "chat-a": chatPanel("a"), "chat-b": chatPanel("b") };
    const onA = makeLayout(panels, { activeView: "chat-a", activeGroup: "group-1" });
    const onB = makeLayout(panels, { activeView: "chat-b", activeGroup: "group-2" });
    expect(layoutContentsAreEquivalent(onA, onB)).toBe(true);
  });

  it("ignores the per-tab terminal id and the url that embeds it", () => {
    const mine = makeLayout({ "terminal-session-work": terminalPanel("work", "term-1111") });
    const theirs = makeLayout({ "terminal-session-work": terminalPanel("work", "term-2222") });
    expect(layoutContentsAreEquivalent(mine, theirs)).toBe(true);
  });

  // A restoring client rebuilds its panel map in the order it recreates panels,
  // which is not the order the saving client inserted them. That is a difference
  // of spelling, and raw JSON equality reports it as a change.
  it("ignores the order panels happen to have been inserted in", () => {
    const layout = makeLayout({ "chat-a": chatPanel("a"), "chat-b": chatPanel("b") });
    const reinserted: SavedLayout = {
      dockview: layout.dockview,
      panelParams: { "chat-b": chatPanel("b"), "chat-a": chatPanel("a") },
    };

    expect(JSON.stringify(reinserted)).not.toBe(JSON.stringify(layout));
    expect(layoutContentsAreEquivalent(layout, reinserted)).toBe(true);
  });

  it("still sees a tab being opened", () => {
    const before = makeLayout({ "chat-a": chatPanel("a") });
    const after = makeLayout({ "chat-a": chatPanel("a"), "chat-b": chatPanel("b") });
    expect(layoutContentsAreEquivalent(before, after)).toBe(false);
  });

  it("still sees a tab being closed", () => {
    const before = makeLayout({ "chat-a": chatPanel("a"), "chat-b": chatPanel("b") });
    const after = makeLayout({ "chat-a": chatPanel("a") });
    expect(layoutContentsAreEquivalent(before, after)).toBe(false);
  });

  it("still sees a terminal tab rebound to a different tmux session", () => {
    const before = makeLayout({ "terminal-session-work": terminalPanel("work", "term-1") });
    const after = makeLayout({ "terminal-session-work": terminalPanel("scratch", "term-1") });
    expect(layoutContentsAreEquivalent(before, after)).toBe(false);
  });

  it("still sees an iframe tab pointed at a different url", () => {
    const before = makeLayout({
      "iframe-1": { panelType: "iframe", agentId: "primary", url: "/service/web/", serviceName: "web" },
    });
    const after = makeLayout({
      "iframe-1": { panelType: "iframe", agentId: "primary", url: "/service/web/reports", serviceName: "web" },
    });
    // A non-terminal iframe's url is real content, unlike a terminal's, which is
    // rebuilt per tab from the session name.
    expect(layoutContentsAreEquivalent(before, after)).toBe(false);
  });

  it("treats an absent field and an explicitly undefined one as the same", () => {
    const withUndefined = makeLayout({
      "chat-a": { panelType: "chat", agentId: "a", chatAgentId: "a", title: "a", serviceName: undefined },
    });
    const without = makeLayout({ "chat-a": chatPanel("a") });
    expect(layoutContentsAreEquivalent(withUndefined, without)).toBe(true);
  });

  it("distinguishes a layout with no content from an empty one", () => {
    expect(layoutContentsAreEquivalent(null, makeLayout({}))).toBe(false);
    expect(layoutContentsAreEquivalent(null, null)).toBe(true);
  });
});
