/**
 * Terminal tab bound to a specific agent.
 *
 * Opening an agent terminal attaches to that agent's tmux session, which does
 * not exist while the agent is STOPPED -- the ttyd dispatch's `tmux attach`
 * fails immediately. So before mounting the terminal iframe this panel POSTs
 * to the agent's start endpoint and waits for it to resolve. The backend
 * no-ops for already-running agents, so this is cheap in the common case.
 *
 * This covers both ways an agent terminal opens: the chat-page "Open agent
 * terminal" link and terminal tabs restored from a saved dockview layout
 * (both routed here by DockviewWorkspace.createComponent).
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { getAgentById } from "../models/AgentManager";
import { isAgentProcessDead } from "./agentLiveness";
import { IframePanel } from "./IframePanel";

interface AgentTerminalPanelAttrs {
  agentId: string;
  url: string;
  title: string;
}

export function AgentTerminalPanel(): m.Component<AgentTerminalPanelAttrs> {
  let starting = true;
  let startError: string | null = null;

  async function ensureAgentStarted(agentId: string): Promise<void> {
    // Defensive: if the panel was constructed without an agentId (e.g. a
    // legacy or corrupt PanelParams entry from a restored layout), there is
    // no agent to start. POSTing to `/api/agents//start` would just 404;
    // skip straight to mounting the iframe with no error banner.
    if (agentId === "") {
      starting = false;
      m.redraw();
      return;
    }
    try {
      const response = await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/start`), {
        method: "POST",
      });
      if (!response.ok) {
        const data = (await response.json().catch(() => ({}))) as { detail?: string };
        startError = data.detail ?? `Failed to start agent (HTTP ${response.status})`;
      }
    } catch (e) {
      startError = (e as Error).message;
    } finally {
      starting = false;
      m.redraw();
    }
  }

  return {
    oninit(vnode) {
      ensureAgentStarted(vnode.attrs.agentId);
    },

    view(vnode) {
      if (starting) {
        return m(
          "div",
          { class: "agent-terminal-starting flex items-center justify-center h-full" },
          m("p", { class: "text-secondary" }, "Starting agent..."),
        );
      }

      // A dead agent has no tmux session, so a mounted ttyd client reconnect-loops
      // against it (spawn `tmux attach`, exit, retry -- several times a second, each
      // attempt claiming the page's focus before the client was patched not to). Same
      // pattern as IframePanel's stopped-service placeholder: unmount the iframe while
      // the agent is positively dead; the agents push after a start swaps it back in.
      // An untracked id or UNKNOWN state keeps the iframe -- non-evidence is not death.
      const agent = vnode.attrs.agentId === "" ? undefined : getAgentById(vnode.attrs.agentId);
      if (agent !== undefined && isAgentProcessDead(agent.state)) {
        return m(
          "div",
          { class: "agent-terminal-stopped flex h-full w-full flex-col items-center justify-center gap-3 bg-surface" },
          [
            m("div", { class: "text-[15px] font-medium text-primary" }, agent.name),
            m(
              "div",
              { class: "text-[13px] text-faint" },
              "This agent is stopped, so its terminal has nothing to attach to.",
            ),
            // A failed start leaves the agent dead, which lands back on this face --
            // without this line the Start button would appear to silently do nothing.
            startError === null
              ? null
              : m(
                  "div",
                  { class: "agent-terminal-start-error text-[13px] text-danger" },
                  `Could not start agent: ${startError}`,
                ),
            m(
              "button",
              {
                type: "button",
                class:
                  "agent-terminal-start mt-1 flex h-8 cursor-pointer items-center rounded-md border " +
                  "border-default px-4 text-[13px] font-medium text-primary hover:bg-fill-hover",
                onclick: () => {
                  starting = true;
                  startError = null;
                  void ensureAgentStarted(vnode.attrs.agentId);
                },
              },
              "Start agent",
            ),
          ],
        );
      }

      // Even if the start attempt errored, still mount the terminal iframe so
      // the user sees ttyd's own output; the error is surfaced just above it.
      if (startError !== null) {
        return m("div", { style: "display: flex; flex-direction: column; height: 100%;" }, [
          m(
            "div",
            {
              class: "agent-terminal-start-error text-danger",
              style: "font-size: 0.85em; padding: 4px 8px; flex: 0 0 auto;",
            },
            `Could not start agent: ${startError}`,
          ),
          m(
            "div",
            { style: "flex: 1 1 auto; min-height: 0;" },
            m(IframePanel, { url: vnode.attrs.url, title: vnode.attrs.title }),
          ),
        ]);
      }

      return m(IframePanel, { url: vnode.attrs.url, title: vnode.attrs.title });
    },
  };
}
