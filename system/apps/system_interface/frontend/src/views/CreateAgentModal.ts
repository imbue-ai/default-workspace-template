/**
 * Modal dialog for creating a new agent (chat, on either harness).
 * Shows a single "Name" input field pre-filled with a random name.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

interface CreateAgentModalAttrs {
  mode: "chat" | "codex" | "pi" | "opencode" | "antigravity" | "silly-claude" | "silly-codex";
  onCreated: (agentId: string, agentName: string) => void;
  onCancel: () => void;
}

export function CreateAgentModal(): m.Component<CreateAgentModalAttrs> {
  let name = "";
  let loading = false;
  let error: string | null = null;

  async function fetchRandomName(): Promise<void> {
    try {
      const response = await m.request<{ name: string }>({
        method: "GET",
        url: apiUrl("/api/random-name"),
      });
      name = response.name;
      m.redraw();
    } catch {
      name = `agent-${Date.now().toString(36)}`;
    }
  }

  async function submit(attrs: CreateAgentModalAttrs): Promise<void> {
    if (!name.trim() || loading) {
      return;
    }
    loading = true;
    error = null;
    m.redraw();

    try {
      // Both modes create the same `chat` role in the primary's work dir; they
      // differ only in which harness template the server stacks under it.
      const urlByMode: Record<string, string> = {
        chat: "/api/agents/create-chat",
        codex: "/api/agents/create-codex",
        pi: "/api/agents/create-pi",
        opencode: "/api/agents/create-opencode",
        antigravity: "/api/agents/create-antigravity",
        "silly-claude": "/api/agents/create-silly-claude",
        "silly-codex": "/api/agents/create-silly-codex",
      };
      const url = apiUrl(urlByMode[attrs.mode]);

      const body: Record<string, string> = { name: name.trim() };

      const response = await m.request<{ agent_id: string }>({
        method: "POST",
        url,
        body,
      });

      attrs.onCreated(response.agent_id, name.trim());
    } catch (e) {
      // mithril attaches the parsed JSON error body to `.response`; the server
      // sends the human-readable reason there as `detail`. Reading `.message`
      // instead surfaces the raw body object as "[object Object]".
      const errResp = (e as { response?: { detail?: string } }).response;
      error = errResp?.detail ?? (e as Error).message ?? "Creation failed";
      loading = false;
      m.redraw();
    }
  }

  return {
    oninit() {
      fetchRandomName();
    },

    view(vnode) {
      const attrs = vnode.attrs;
      const titleByMode: Record<string, string> = {
        chat: "Create Chat Agent",
        codex: "Create Codex Agent",
        pi: "Create Pi Agent",
        opencode: "Create Opencode Agent",
        antigravity: "Create Antigravity Agent",
        "silly-claude": "Create Silly Claude Agent",
        "silly-codex": "Create Silly Codex Agent",
      };
      const title = titleByMode[attrs.mode];

      return m(
        "div.custom-url-dialog-overlay",
        {
          onclick(e: MouseEvent) {
            if ((e.target as HTMLElement).classList.contains("custom-url-dialog-overlay")) {
              attrs.onCancel();
            }
          },
        },
        [
          m(
            "div.custom-url-dialog",
            {
              onclick(e: MouseEvent) {
                e.stopPropagation();
              },
            },
            [
              m("h3.custom-url-dialog-title", title),
              m("label.custom-url-dialog-label", "Agent Name"),
              m("input.custom-url-dialog-input", {
                type: "text",
                value: name,
                placeholder: "agent-name",
                autofocus: true,
                oninput(e: InputEvent) {
                  name = (e.target as HTMLInputElement).value;
                },
                onkeydown(e: KeyboardEvent) {
                  if (e.key === "Enter") {
                    submit(attrs);
                  }
                  if (e.key === "Escape") {
                    attrs.onCancel();
                  }
                },
              }),
              error ? m("p", { style: "color: red; font-size: 0.85em; margin-top: 4px;" }, error) : null,
              m("div.custom-url-dialog-actions", [
                m(
                  "button.custom-url-dialog-cancel",
                  {
                    onclick: attrs.onCancel,
                    disabled: loading,
                  },
                  "Cancel",
                ),
                m(
                  "button.custom-url-dialog-open",
                  {
                    onclick: () => submit(attrs),
                    disabled: loading || !name.trim(),
                  },
                  loading ? "Creating..." : "Create",
                ),
              ]),
            ],
          ),
        ],
      );
    },
  };
}
