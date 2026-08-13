// The workspace shell: a sandboxed cross-origin iframe on the workspace's
// share URL (owner entry is silent via the broker fast path), with a
// new-tab fallback for browsers where partitioned iframe cookies fail.
// Also finishes a pending create's in-workspace setup (backups) once the
// iframe's owner handoff has established the workspace session.

import m from "mithril";
import { shareStatus } from "../api";
import { type CreateProgress, completePendingSetup } from "../createflow";
import { probeWorkspaceHealth, workspaceEntryHost } from "../exec";
import {
  type EmbedderEndpoint,
  OPEN_AI_KEYS_ACK,
  createEmbedder,
} from "../embedshim";
import { type PendingCreate, loadPendingCreates } from "../records";
import { MintModal } from "./mint";

const OWNER_SESSION_POLL_MS = 2000;
const OWNER_SESSION_POLL_LIMIT = 60;

export function WorkspaceView(): m.Component<{ hostId: string }> {
  let workspaceDomain: string | null = null;
  // The routable entry host (shell label origin; the bare domain is unrouted
  // on the relay). Set together with workspaceDomain.
  let entryHost: string | null = null;
  let error = "";
  let mintHostId: string | null = null;
  let pending: PendingCreate | null = null;
  let setupProgress: CreateProgress | null = null;
  let embedder: EmbedderEndpoint | null = null;
  let frame: HTMLIFrameElement | null = null;
  let hostId = "";

  // The workspace domain family: the iframe's own document lives on the bare
  // share domain (that is where the embed-contract messages come from) and
  // its services live on subdomains of it.
  function isWorkspaceOrigin(origin: string): boolean {
    if (workspaceDomain === null) return false;
    return (
      origin === `https://${workspaceDomain}` ||
      origin.endsWith(`.${workspaceDomain}`)
    );
  }

  async function finishSetupWhenSessionReady(): Promise<void> {
    if (pending === null || entryHost === null) return;
    for (let attempt = 0; attempt < OWNER_SESSION_POLL_LIMIT; attempt++) {
      const health = await probeWorkspaceHealth(entryHost);
      if (health.detail?.owner === true) {
        try {
          await completePendingSetup(pending, (update) => {
            setupProgress = update;
            m.redraw();
          });
          pending = null;
          setupProgress = null;
        } catch (setupError) {
          setupProgress = null;
          error = `Workspace setup did not finish: ${String(setupError)} (it will retry when you reopen this workspace)`;
        }
        m.redraw();
        return;
      }
      await new Promise((resolve) =>
        setTimeout(resolve, OWNER_SESSION_POLL_MS),
      );
    }
  }

  async function load(): Promise<void> {
    const status = await shareStatus(hostId).catch(() => null);
    if (status === null || status.state !== "active") {
      error = "This workspace is not shared (no web access).";
      m.redraw();
      return;
    }
    workspaceDomain = status.workspace_domain;
    entryHost = workspaceEntryHost(status.workspace_domain, status.entry_label);
    const pendings = await loadPendingCreates();
    pending = pendings.find((entry) => entry.host_id === hostId) ?? null;
    m.redraw();
    if (pending !== null) {
      void finishSetupWhenSessionReady();
    }
  }

  return {
    oninit(vnode) {
      hostId = vnode.attrs.hostId;
      void load();
    },
    onremove() {
      embedder?.dispose();
      embedder = null;
    },
    view() {
      if (error && workspaceDomain === null) {
        return m(
          "div",
          { class: "p-6" },
          m("p", { class: "text-sm text-red-600" }, error),
        );
      }
      if (workspaceDomain === null || entryHost === null) {
        return m(
          "div",
          { class: "p-6 text-slate-500" },
          "Loading workspace...",
        );
      }
      const workspaceUrl = `https://${entryHost}/`;
      return m(
        "div",
        { class: "flex grow flex-col" },
        m(
          "div",
          {
            class:
              "flex items-center gap-3 border-b border-slate-200 dark:border-slate-800 px-4 py-2 text-sm",
          },
          m("span", { class: "text-slate-500 truncate" }, workspaceDomain),
          setupProgress
            ? m(
                "span",
                { class: "text-amber-600" },
                `Finishing setup: ${setupProgress.message}`,
              )
            : null,
          error ? m("span", { class: "text-red-600 truncate" }, error) : null,
          m("div", { class: "grow" }),
          m(
            "a",
            {
              class: "underline text-slate-500",
              href: workspaceUrl,
              target: "_blank",
              rel: "noopener",
            },
            "Open in new tab",
          ),
        ),
        m("iframe", {
          class: "grow w-full border-0",
          src: workspaceUrl,
          allow: "clipboard-read; clipboard-write",
          oncreate(vnode: m.VnodeDOM) {
            frame = vnode.dom as HTMLIFrameElement;
            embedder = createEmbedder(
              () => frame?.contentWindow ?? null,
              isWorkspaceOrigin,
              {
                onOpenAiKeysPage(requestedHostId) {
                  mintHostId = requestedHostId ?? hostId;
                  embedder?.send(OPEN_AI_KEYS_ACK);
                  m.redraw();
                },
              },
            );
          },
        }),
        mintHostId !== null
          ? m(MintModal, {
              hostId: mintHostId,
              onClose() {
                mintHostId = null;
              },
            })
          : null,
      );
    },
  };
}
