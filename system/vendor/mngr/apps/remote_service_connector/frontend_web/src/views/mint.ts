// The hosted mint modal: mints (rotating) the workspace's LiteLLM key and
// renders the paste-ready env blob for the in-workspace sign-in modal.

import m from "mithril";
import { mintWorkspaceKey } from "../api";

export interface MintModalAttrs {
  hostId: string;
  onClose: () => void;
}

export const MintModal: m.Component<MintModalAttrs> = {
  view(vnode) {
    return m(MintModalBody, vnode.attrs);
  },
};

const MintModalBody: m.ClosureComponent<MintModalAttrs> = () => {
  let blob: string | null = null;
  let error = "";
  let busy = false;
  let copied = false;

  async function mint(hostId: string): Promise<void> {
    busy = true;
    error = "";
    m.redraw();
    try {
      const result = await mintWorkspaceKey(hostId);
      blob = `ANTHROPIC_BASE_URL=${result.base_url}\nANTHROPIC_API_KEY=${result.key}\n`;
    } catch (mintError) {
      error = `Mint failed: ${String(mintError)}`;
    } finally {
      busy = false;
      m.redraw();
    }
  }

  return {
    view(vnode) {
      return m(
        "div",
        {
          class:
            "fixed inset-0 z-50 flex items-center justify-center bg-black/50",
          onclick(event: MouseEvent) {
            if (event.target === event.currentTarget) vnode.attrs.onClose();
          },
        },
        m(
          "div",
          {
            class:
              "w-full max-w-md rounded-lg bg-white dark:bg-slate-900 p-6 space-y-4",
          },
          m("h2", { class: "text-lg font-semibold" }, "Sign in with Imbue"),
          m(
            "p",
            { class: "text-sm text-slate-500" },
            "Mints an inference key for this workspace against your account " +
              "(any previously minted key for it stops working) and gives you " +
              "a block to paste into the workspace's sign-in dialog.",
          ),
          blob === null
            ? m(
                "button",
                {
                  class:
                    "rounded bg-slate-900 dark:bg-slate-100 text-white dark:text-slate-900 px-4 py-2",
                  disabled: busy,
                  onclick: () => void mint(vnode.attrs.hostId),
                },
                busy ? "Minting..." : "Mint key",
              )
            : m(
                "div",
                { class: "space-y-2" },
                m(
                  "pre",
                  {
                    class:
                      "rounded bg-slate-100 dark:bg-slate-800 p-3 text-xs overflow-x-auto select-all",
                  },
                  blob,
                ),
                m(
                  "button",
                  {
                    class:
                      "rounded border border-slate-300 dark:border-slate-700 px-3 py-1 text-sm",
                    async onclick() {
                      await navigator.clipboard.writeText(blob ?? "");
                      copied = true;
                      m.redraw();
                    },
                  },
                  copied ? "Copied" : "Copy",
                ),
              ),
          error ? m("p", { class: "text-sm text-red-600" }, error) : null,
          m(
            "div",
            { class: "text-right" },
            m(
              "button",
              {
                class: "text-sm text-slate-500 underline",
                onclick: () => vnode.attrs.onClose(),
              },
              "Close",
            ),
          ),
        ),
      );
    },
  };
};
