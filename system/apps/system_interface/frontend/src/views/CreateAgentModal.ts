/**
 * Modal dialog for creating a new agent (worktree or chat).
 * Shows a single "Name" input field pre-filled with a random name.
 */

import m from "mithril";
import { apiUrl, getPrimaryAgentId } from "../base-path";
import { fetchRandomAgentName } from "../models/AgentManager";

interface CreateAgentModalAttrs {
  mode: "worktree" | "chat";
  onCreated: (agentId: string, agentName: string) => void;
  onCancel: () => void;
}

export function CreateAgentModal(): m.Component<CreateAgentModalAttrs> {
  let name = "";
  let loading = false;
  let error: string | null = null;

  /** Pre-fill the input with a fresh name. The generator is shared with the
   *  chat a new project is made with, so both surfaces name agents alike. */
  async function fetchRandomName(): Promise<void> {
    name = await fetchRandomAgentName();
    m.redraw();
  }

  async function submit(attrs: CreateAgentModalAttrs): Promise<void> {
    if (!name.trim() || loading) {
      return;
    }
    loading = true;
    error = null;
    m.redraw();

    try {
      const url =
        attrs.mode === "worktree" ? apiUrl("/api/agents/create-worktree") : apiUrl("/api/agents/create-chat");

      const body: Record<string, string> =
        attrs.mode === "worktree"
          ? { name: name.trim(), selected_agent_id: getPrimaryAgentId() }
          : { name: name.trim() };

      const response = await m.request<{ agent_id: string }>({
        method: "POST",
        url,
        body,
      });

      attrs.onCreated(response.agent_id, name.trim());
    } catch (e) {
      error = (e as Error).message ?? "Creation failed";
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
      const title = attrs.mode === "worktree" ? "Create Worktree Agent" : "Create Chat Agent";

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
