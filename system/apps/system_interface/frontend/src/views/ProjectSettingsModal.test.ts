import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, which
// makes the `m.redraw()` calls inside the modal's event handlers throw.
// Provide a polyfill before any import is evaluated, as ClaudeLoginModal's
// test does for the same reason.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// apiUrl reads the base path from a <meta> tag, which vitest's node
// environment has no document for; identity keeps the asserted URLs the bare
// /api paths (mirrors Projects.test.ts).
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

// The modal's own member-name resolution reaches into AgentManager's module
// state (fed by the WebSocket in a live workspace) for a chat's display name
// and the machine's app list. Swap it for a mutable stand-in so each case can
// pose a different machine, the same idiom AllAppsPicker.test.ts and
// Sidebar.test.ts use for the same module.
const agentManagerState: {
  agentById: Record<string, { name: string; display_name?: string | null }>;
  apps: { name: string; url: string; label: string; icon?: string }[];
  protoAgents: { agent_id: string; name: string }[];
} = { agentById: {}, apps: [], protoAgents: [] };

vi.mock("../models/AgentManager", () => ({
  getApps: () => agentManagerState.apps,
  getAgentById: (id: string) => agentManagerState.agentById[id],
  getProtoAgents: () => agentManagerState.protoAgents,
}));

import { applyMemberTitleChange, loadMemberTitles } from "../models/MemberTitles";
import type { ProjectInfo } from "../models/Projects";
import { ProjectSettingsModal } from "./ProjectSettingsModal";
import type { ProjectSettingsModalAttrs } from "./ProjectSettingsModal";

const PROJECT: ProjectInfo = {
  project_id: "research",
  name: "Research",
  color: "#4f8ef7",
  glyph: 3,
  has_content: true,
  members: ["chat:agent-9", "terminal:terminal-4", "service:browser?session=quiet-otter", "service:docs", "url:9f86"],
};

type VnodeLike = {
  attrs?: Record<string, unknown>;
  children?: unknown;
  tag?: unknown;
};

/** Depth-first walk over a rendered Mithril vnode tree (mirrors
 *  ClaudeLoginModal.test.ts's helper, which every modal test in this
 *  directory that inspects a tree without mounting it re-implements). */
function* walk(node: unknown): Generator<VnodeLike> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (node !== null && typeof node === "object") {
    const vnode = node as VnodeLike;
    yield vnode;
    if (vnode.children !== undefined) yield* walk(vnode.children);
  }
}

function findByClass(tree: unknown, className: string): VnodeLike | undefined {
  for (const vnode of walk(tree)) {
    const classes = vnode.attrs?.className;
    if (typeof classes === "string" && classes.split(/\s+/).includes(className)) {
      return vnode;
    }
  }
  return undefined;
}

/** Every `button` vnode whose rendered text (once trusted markup is stripped
 *  away) contains `text`. */
function buttonsByText(tree: unknown, text: string): VnodeLike[] {
  const matches: VnodeLike[] = [];
  for (const vnode of walk(tree)) {
    if (
      typeof vnode.tag === "string" &&
      vnode.tag.startsWith("button") &&
      typeof vnode.attrs?.onclick === "function"
    ) {
      const label = vnode.attrs["aria-label"];
      if (typeof label === "string" && label.includes(text)) matches.push(vnode);
    }
  }
  return matches;
}

function clickVnode(vnode: VnodeLike): void {
  (vnode.attrs?.onclick as () => void)();
}

/** Stand in for the backend with one canned response for every call. */
function stubFetch(response: Partial<Response>): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn(() => Promise.resolve(response as Response));
  vi.stubGlobal("fetch", mockFetch);
  return mockFetch;
}

// Let queued microtasks + the setTimeout-based redraw polyfill drain.
const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function makeModal(attrs: ProjectSettingsModalAttrs): { render: () => unknown } {
  const component = ProjectSettingsModal();
  const vnode = { attrs };
  if (component.oninit) component.oninit(vnode as Parameters<NonNullable<typeof component.oninit>>[0]);
  return {
    render: () => component.view(vnode as Parameters<typeof component.view>[0]),
  };
}

beforeEach(async () => {
  agentManagerState.agentById = {};
  agentManagerState.apps = [];
  agentManagerState.protoAgents = [];
  // Reset the member-titles module cache, the same way MemberTitles.test.ts does.
  stubFetch({ ok: true, json: () => Promise.resolve({ titles: {} }) });
  await loadMemberTitles();
  vi.unstubAllGlobals();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ProjectSettingsModal member list", () => {
  it("shows an empty state for a project with no members", () => {
    const modal = makeModal({
      project: { ...PROJECT, members: [] },
      onSaved: () => {},
      onDeleted: () => {},
      onCancel: () => {},
      onMemberRemoved: () => {},
    });

    expect(JSON.stringify(modal.render())).toContain("No members yet.");
  });

  it("labels each member with its derived name when it was never renamed", () => {
    agentManagerState.agentById["agent-9"] = { name: "Chat-9", display_name: "Chat 9" };

    const modal = makeModal({
      project: PROJECT,
      onSaved: () => {},
      onDeleted: () => {},
      onCancel: () => {},
      onMemberRemoved: () => {},
    });
    const tree = JSON.stringify(modal.render());

    expect(tree).toContain("Chat 9");
    expect(tree).toContain("Terminal 4");
    expect(tree).toContain("Browser quiet-otter");
    expect(tree).toContain("docs");
    expect(tree).toContain("Page");
  });

  it("prefers a chosen member-titles name over the derived one", () => {
    applyMemberTitleChange("terminal:terminal-4", "Build Server");

    const modal = makeModal({
      project: PROJECT,
      onSaved: () => {},
      onDeleted: () => {},
      onCancel: () => {},
      onMemberRemoved: () => {},
    });
    const tree = JSON.stringify(modal.render());

    expect(tree).toContain("Build Server");
    expect(tree).not.toContain("Terminal 4");
  });

  it("removes a member from the working list once the server confirms it", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ project_id: "research", members: [] }) });
    const modal = makeModal({
      project: PROJECT,
      onSaved: () => {},
      onDeleted: () => {},
      onCancel: () => {},
      onMemberRemoved: () => {},
    });
    expect(JSON.stringify(modal.render())).toContain("service:docs");

    const removeButtons = buttonsByText(modal.render(), "Remove");
    const docsRemoveButton = removeButtons.find((button) => (button.attrs?.["aria-label"] as string).includes("docs"));
    expect(docsRemoveButton).toBeDefined();
    clickVnode(docsRemoveButton!);
    await flush();

    expect(mockFetch).toHaveBeenCalledWith("/api/projects/research/members/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "service:docs" }),
    });
    const tree = JSON.stringify(modal.render());
    expect(tree).not.toContain("service:docs");
  });

  it("keeps the member and surfaces an error when the server refuses the removal", async () => {
    stubFetch({ ok: false, status: 404, json: () => Promise.resolve({ detail: "Project 'research' not found" }) });
    const modal = makeModal({
      project: PROJECT,
      onSaved: () => {},
      onDeleted: () => {},
      onCancel: () => {},
      onMemberRemoved: () => {},
    });

    const removeButtons = buttonsByText(modal.render(), "Remove");
    const docsRemoveButton = removeButtons.find((button) => (button.attrs?.["aria-label"] as string).includes("docs"));
    clickVnode(docsRemoveButton!);
    await flush();

    const tree = JSON.stringify(modal.render());
    expect(tree).toContain("service:docs");
    expect(tree).toContain("Project 'research' not found");
  });
});

describe("ProjectSettingsModal delete confirmation", () => {
  it("describes deleting as removing the view only, with members left running", () => {
    const modal = makeModal({
      project: PROJECT,
      onSaved: () => {},
      onDeleted: () => {},
      onCancel: () => {},
      onMemberRemoved: () => {},
    });
    const deleteButton = findByClass(modal.render(), "destroy-dialog-btn-cancel");
    expect(deleteButton).toBeDefined();
    clickVnode(deleteButton!);

    const tree = JSON.stringify(modal.render());
    expect(tree).toContain("removes the view only");
    expect(tree).toContain("keeps running");
    expect(tree).toContain("Everything");
    // The old, now-inaccurate warning about stopping terminals and browsers is gone.
    expect(tree).not.toContain("shut down");
  });
});
