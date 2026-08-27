/**
 * The provider chooser, and the sign-in screens behind it.
 *
 * Two levels. The chooser lists the providers you can sign in to, one row each. Picking one
 * opens its sign-in screen: the recommended way in at the top, and any alternates under a
 * disclosure -- the shape the Claude modal already had, generalised so every provider can
 * have its own alternates.
 *
 * The sign-in screen renders one of three shapes, and the server tells us which per method,
 * so nothing here knows what a harness is. That indirection is load-bearing rather than
 * fussy: OpenAI inverts the usual flow (it prints a code you type into the browser, and
 * nothing comes back to the terminal), and no property of "OpenAI" as a provider would have
 * told the client that.
 *
 * Reuses the `.claude-login-*` styles wholesale -- same chrome, same rows, same buttons --
 * rather than introducing a parallel vocabulary for an identical modal.
 */

import m from "mithril";

import { icon } from "./icons";
import type { Lane, LaneMethod } from "../models/Providers";
import {
  abortFlow,
  areLanesLoaded,
  clearFlow,
  getAccounts,
  getFlow,
  getLanes,
  loadAccounts,
  loadLanes,
  startFlow,
  submitCode,
  submitKey,
} from "../models/Providers";

export interface ProviderChooserModalAttrs {
  onDismiss: () => void;
}

export function ProviderChooserModal(): m.Component<ProviderChooserModalAttrs> {
  let lane: Lane | null = null;
  let showAlternates = false;
  let codeInput = "";
  let keyInput = "";
  let keyProvider: string | null = null;
  let error: string | null = null;
  let busy = false;

  function reset(): void {
    lane = null;
    showAlternates = false;
    codeInput = "";
    keyInput = "";
    keyProvider = null;
    error = null;
    clearFlow();
  }

  async function begin(chosen: Lane, chosenMethod: LaneMethod): Promise<void> {
    lane = chosen;
    error = null;
    // A key flow needs a provider chosen before it can be written; default to the lane's
    // first so the common single-provider case needs no extra click.
    keyProvider = chosen.key_providers.length > 0 ? chosen.key_providers[0].provider_id : null;
    busy = true;
    m.redraw();
    try {
      await startFlow(chosen.id, chosenMethod.id);
    } catch (e) {
      error = errorText(e);
    } finally {
      busy = false;
      m.redraw();
    }
  }

  async function send(action: () => Promise<void>): Promise<void> {
    busy = true;
    error = null;
    m.redraw();
    try {
      await action();
    } catch (e) {
      error = errorText(e);
    } finally {
      busy = false;
      m.redraw();
    }
  }

  function back(): void {
    abortFlow();
    reset();
    m.redraw();
  }

  function renderChooser(): m.Children {
    if (!areLanesLoaded()) return m("div.claude-login-applying", "Loading providers...");
    return m(
      "div.claude-login-alts-list",
      getLanes().map((candidate) =>
        m(
          "button.claude-login-alt",
          {
            type: "button",
            key: candidate.id,
            onclick: () => begin(candidate, candidate.methods[0]),
          },
          [
            m("span.claude-login-alt-text", [
              m("span.claude-login-alt-name", candidate.provider_name),
              m("span.claude-login-alt-desc", candidate.subtitle),
            ]),
            m("span.claude-login-alt-go", m.trust(icon("chevron-right", { size: 16 }))),
          ],
        ),
      ),
    );
  }

  function renderAlternates(current: Lane): m.Children {
    const others = current.methods.filter((candidate) => !candidate.is_primary);
    if (others.length === 0) return null;
    return m("div.claude-login-alts", [
      m(
        "button.claude-login-alts-toggle",
        { type: "button", onclick: () => (showAlternates = !showAlternates) },
        `Other ways to sign in${showAlternates ? "" : ` (${others.length})`}`,
      ),
      showAlternates
        ? m(
            "div.claude-login-alts-list",
            others.map((candidate) =>
              m(
                "button.claude-login-alt",
                { type: "button", key: candidate.id, onclick: () => begin(current, candidate) },
                [
                  m("span.claude-login-alt-text", [
                    m("span.claude-login-alt-name", candidate.label),
                    m("span.claude-login-alt-desc", candidate.description),
                  ]),
                  m("span.claude-login-alt-go", m.trust(icon("chevron-right", { size: 16 }))),
                ],
              ),
            ),
          )
        : null,
    ]);
  }

  function renderSignIn(current: Lane): m.Children {
    const flow = getFlow();
    if (flow !== null && flow.status.state === "ok") {
      return m("div.claude-login-applying", `Signed in to ${current.provider_name}.`);
    }
    if (busy && flow === null) return m("div.claude-login-applying", "Starting sign-in...");
    if (flow === null) return null;

    if (flow.shape === "paste") return renderPaste(current);
    return [
      m("p", "Open this link, sign in, and come back:"),
      flow.url !== null
        ? m(
            "a.claude-login-button.claude-login-button--primary.claude-login-button--block",
            {
              href: flow.url,
              target: "_blank",
              rel: "noreferrer",
            },
            "Open the sign-in page",
          )
        : null,
      flow.shape === "code_then_wait" ? renderWaitForCode(flow.code) : renderPasteCode(),
    ];
  }

  function renderWaitForCode(code: string | null): m.Children {
    // The inverted shape: the code is ours to show, and the user types it in the browser.
    // Nothing comes back here, so there is no input -- we poll until the CLI says it is in.
    return [
      m("p", "Enter this one-time code on that page:"),
      m("div.claude-login-code", code ?? ""),
      m("div.claude-login-applying", "Waiting for you to finish in the browser..."),
    ];
  }

  function renderPasteCode(): m.Children {
    return [
      m("p", "Then paste the code it gives you:"),
      m("input.claude-login-input", {
        type: "text",
        value: codeInput,
        placeholder: "authorization code",
        oninput: (e: Event) => (codeInput = (e.target as HTMLInputElement).value),
        onkeydown: (e: KeyboardEvent) => {
          if (e.key === "Enter" && codeInput.trim() !== "") send(() => submitCode(codeInput.trim()));
        },
      }),
      m(
        "button.claude-login-button.claude-login-button--primary",
        {
          type: "button",
          disabled: busy || codeInput.trim() === "",
          onclick: () => send(() => submitCode(codeInput.trim())),
        },
        busy ? "Verifying..." : "Verify code",
      ),
    ];
  }

  function renderPaste(current: Lane): m.Children {
    const choices = current.key_providers;
    const selected = choices.find((candidate) => candidate.provider_id === keyProvider) ?? null;
    return [
      // Only worth a picker when there is a choice; the single-provider lanes skip it.
      choices.length > 1
        ? m(
            "select.claude-login-input",
            {
              value: keyProvider ?? "",
              onchange: (e: Event) => (keyProvider = (e.target as HTMLSelectElement).value),
            },
            choices.map((candidate) =>
              m("option", { value: candidate.provider_id, key: candidate.provider_id }, candidate.display),
            ),
          )
        : null,
      m("input.claude-login-input", {
        type: "password",
        value: keyInput,
        placeholder: selected?.hint || "API key",
        oninput: (e: Event) => (keyInput = (e.target as HTMLInputElement).value),
        onkeydown: (e: KeyboardEvent) => {
          if (e.key === "Enter" && keyInput.trim() !== "") send(() => submitKey(keyInput.trim(), keyProvider));
        },
      }),
      selected !== null && selected.env_var !== ""
        ? m("p.claude-login-alt-desc", `Saved as ${selected.env_var} for this mind.`)
        : null,
      m(
        "button.claude-login-button.claude-login-button--primary",
        {
          type: "button",
          disabled: busy || keyInput.trim() === "",
          onclick: () => send(() => submitKey(keyInput.trim(), keyProvider)),
        },
        busy ? "Saving..." : "Save key",
      ),
    ];
  }

  return {
    oninit() {
      loadLanes()
        .then(() => loadAccounts())
        .then(() => m.redraw())
        .catch(() => m.redraw());
    },

    onremove() {
      // A modal closed mid-flow must not leave a CLI waiting on a browser tab that is gone.
      abortFlow();
    },

    view(vnode) {
      const onClose = (): void => {
        abortFlow();
        reset();
        vnode.attrs.onDismiss();
      };
      const flow = getFlow();
      const detail = flow?.status.state === "failed" ? flow.status.detail : null;
      return m(
        "div.claude-login-overlay",
        {
          onclick: (e: MouseEvent) => {
            if (e.target === e.currentTarget) onClose();
          },
        },
        m("div.claude-login-modal", { role: "dialog", "aria-modal": "true", "aria-label": "Pick your AI provider" }, [
          m("div.claude-login-header", [
            lane !== null
              ? m(
                  "button.claude-login-close",
                  { type: "button", onclick: back, "aria-label": "Back" },
                  m.trust(icon("chevron-left", { size: 16 })),
                )
              : null,
            m("h2.claude-login-title", lane === null ? "Pick your AI provider" : lane.provider_name),
            m(
              "button.claude-login-close",
              { type: "button", onclick: onClose, "aria-label": "Close" },
              m.trust(icon("close", { size: 16 })),
            ),
          ]),
          m("div.claude-login-body", [
            error !== null || detail !== null ? m("div.claude-login-error", error ?? detail) : null,
            lane === null ? renderChooser() : [renderSignIn(lane), renderAlternates(lane)],
            lane === null && getAccounts().length > 0
              ? m("p.claude-login-alt-desc", `${getAccounts().length} provider(s) already signed in.`)
              : null,
          ]),
        ]),
      );
    },
  };
}

function errorText(e: unknown): string {
  const response = (e as { response?: { detail?: string } })?.response;
  if (response?.detail) return response.detail;
  return (e as Error)?.message ?? "Something went wrong.";
}
