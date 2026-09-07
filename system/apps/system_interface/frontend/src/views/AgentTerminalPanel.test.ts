// @vitest-environment jsdom
/**
 * The agent-terminal pane's liveness gate: a positively-dead agent gets the stopped
 * face instead of a mounted ttyd iframe (which would reconnect-loop against the missing
 * tmux session), UNKNOWN keeps the iframe (non-evidence is not death), and a start that
 * FAILS while the agent stays dead must say so on the stopped face -- the face is what
 * renders after the failure, so without the error line the Start button would appear to
 * silently do nothing.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

const agentState: { agent: unknown } = { agent: null };
vi.mock("../models/AgentManager", () => ({ getAgentById: () => agentState.agent }));

// The real IframePanel manages live-surface bookkeeping this test has no use for; the
// stub just marks where the terminal iframe would mount.
vi.mock("./IframePanel", () => ({
  IframePanel: {
    view: () => m("div", { class: "iframe-panel-stub" }),
  },
}));

import m from "mithril";

import { AgentTerminalPanel } from "./AgentTerminalPanel";

const ATTRS = { agentId: "agent-1", url: "http://localhost/terminal/", title: "terminal" };

function mountPanel(): HTMLElement {
  const root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, { view: () => m(AgentTerminalPanel, ATTRS) });
  return root;
}

async function settle(): Promise<void> {
  // Let the start fetch resolve and the scheduled redraw (a rAF -> setTimeout here) run.
  await new Promise((resolve) => setTimeout(resolve, 5));
  m.redraw.sync();
}

describe("the agent terminal's liveness gate", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    agentState.agent = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })),
    );
  });

  it("unmounts the iframe for a positively-dead agent and offers a Start button", async () => {
    agentState.agent = { id: "agent-1", name: "sunny-hollow", state: "STOPPED" };
    const root = mountPanel();
    await settle();
    expect(root.querySelector(".agent-terminal-stopped")).not.toBeNull();
    expect(root.querySelector(".iframe-panel-stub")).toBeNull();
    expect(root.querySelector(".agent-terminal-start")).not.toBeNull();
    expect(root.textContent).toContain("sunny-hollow");
  });

  it("keeps the iframe for a live agent and for UNKNOWN (non-evidence is not death)", async () => {
    for (const state of ["RUNNING", "UNKNOWN"]) {
      document.body.innerHTML = "";
      agentState.agent = { id: "agent-1", name: "sunny-hollow", state };
      const root = mountPanel();
      await settle();
      expect(root.querySelector(".iframe-panel-stub"), state).not.toBeNull();
      expect(root.querySelector(".agent-terminal-stopped"), state).toBeNull();
    }
  });

  it("surfaces a failed start on the stopped face instead of failing silently", async () => {
    agentState.agent = { id: "agent-1", name: "sunny-hollow", state: "STOPPED" };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 500, json: async () => ({ detail: "no such agent" }) })),
    );
    const root = mountPanel();
    await settle();

    // The auto-start on open already failed; the face must say so.
    expect(root.querySelector(".agent-terminal-stopped")).not.toBeNull();
    expect(root.querySelector(".agent-terminal-start-error")?.textContent).toContain("no such agent");

    // A retry from the button that fails again keeps reporting, not silently resetting.
    (root.querySelector(".agent-terminal-start") as HTMLElement).click();
    await settle();
    expect(root.querySelector(".agent-terminal-start-error")?.textContent).toContain("no such agent");
  });
});
