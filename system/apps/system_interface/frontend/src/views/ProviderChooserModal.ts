/**
 * The provider chooser, and the sign-in screens behind it.
 *
 * Two levels, following the mockup's grammar. The chooser is a scroll region of uniform
 * rows -- brand mark, name, subtitle, chevron -- and picking one drills into that
 * provider's sign-in with a back chevron in the header. The scroll offset is remembered
 * across a drill-in and back, so returning lands you where you were rather than at the
 * top; the chooser unmounts while a sign-in is up, so that has to live at module scope.
 *
 * A sign-in renders one of three shapes, and the server says which per method, so nothing
 * here knows what a harness is. The shapes are genuinely different flows, not styling
 * variants, which is why the numbered-step layout is shared and only what sits inside the
 * steps changes:
 *
 *   url_then_code   1. open the link  2. paste the code it gives you
 *   code_then_wait  1. open the link  2. type the code WE show into the browser, and wait
 *   paste           no browser at all -- one field, paste a key
 *
 * OpenAI inverts the usual direction (it prints a code you carry to the browser, and
 * nothing comes back to the terminal), which no property of "OpenAI" as a provider would
 * have told the client -- hence the server-supplied shape.
 *
 * Reuses the `.claude-login-*` styles wholesale: same chrome, same rows, same steps, same
 * copy-link fallback. The old modal's markup was the reference for all of it.
 */

import m from "mithril";

import { icon } from "./icons";
import { providerMark } from "./providerMarks";
import type { Lane, LaneMethod, ProviderAccount } from "../models/Providers";
import {
  abortFlow,
  areLanesLoaded,
  clearFlow,
  deleteAccount,
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

/** The chooser's last scroll offset, kept across a drill-in and back so you land where
 *  you were. The chooser's DOM unmounts while a sign-in is showing, so a component-level
 *  ref would not survive; module scope is what makes it outlast the remount. */
let savedScroll = 0;

/** Big enough to read a provider list without the panel growing as lanes are added. */
const SCROLL_REGION_STYLE = "max-height: 332px; overflow-y: auto; overscroll-behavior: contain;";

export function ProviderChooserModal(): m.Component<ProviderChooserModalAttrs> {
  let lane: Lane | null = null;
  let method: LaneMethod | null = null;
  let showAlternates = false;
  let codeInput = "";
  let keyInput = "";
  let keyProvider: string | null = null;
  let error: string | null = null;
  let busy = false;
  // The account being re-authenticated, if this is one. Keeping the folder is the point:
  // every chat bound to it by label comes back rather than being orphaned by an expiry.
  let reauthAccountId: string | null = null;
  let confirmingDelete: string | null = null;
  // Which numbered step is lit. Advances when the link is opened, so the eye follows the
  // flow rather than both steps competing for attention.
  let activeStep: 1 | 2 = 1;
  // Clipboard state for the "didn't open?" fallback. A rejected write (insecure context,
  // denied permission) reveals the raw value so the user is never stranded.
  let copied: "" | "link" | "code" = "";
  let copyFailed = false;

  function reset(): void {
    lane = null;
    method = null;
    showAlternates = false;
    codeInput = "";
    keyInput = "";
    keyProvider = null;
    error = null;
    reauthAccountId = null;
    confirmingDelete = null;
    activeStep = 1;
    copied = "";
    copyFailed = false;
    clearFlow();
  }

  async function copyToClipboard(value: string, kind: "link" | "code"): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      copied = kind;
      copyFailed = false;
    } catch {
      copied = "";
      copyFailed = true;
    }
    m.redraw();
  }

  async function begin(chosen: Lane, chosenMethod: LaneMethod, intoAccountId?: string): Promise<void> {
    lane = chosen;
    method = chosenMethod;
    error = null;
    activeStep = 1;
    copied = "";
    copyFailed = false;
    reauthAccountId = intoAccountId ?? null;
    // A key flow needs a provider before it can be written; default to the lane's first so
    // the single-provider case needs no extra click.
    keyProvider = chosen.key_providers.length > 0 ? chosen.key_providers[0].provider_id : null;
    busy = true;
    m.redraw();
    try {
      await startFlow(chosen.id, chosenMethod.id, intoAccountId);
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

  /** One uniform chooser row: mark, name, subtitle, chevron. */
  function laneRow(candidate: Lane): m.Vnode {
    return m(
      "button.claude-login-alt",
      {
        type: "button",
        key: candidate.id,
        onclick: () => begin(candidate, candidate.methods[0]),
      },
      [
        m("span.claude-login-alt-mark", m.trust(providerMark(candidate.id, 28))),
        m("span.claude-login-alt-text", [
          m("span.claude-login-alt-name", candidate.provider_name),
          m("span.claude-login-alt-desc", candidate.subtitle),
        ]),
        m("span.claude-login-alt-go", m.trust(icon("chevron-right", { size: 16 }))),
      ],
    );
  }

  function renderChooser(): m.Children {
    if (!areLanesLoaded()) return m("div.claude-login-applying", "Loading providers...");
    return m(
      "div",
      {
        style: SCROLL_REGION_STYLE,
        oncreate: (vnode: m.VnodeDOM) => {
          (vnode.dom as HTMLElement).scrollTop = savedScroll;
        },
        onscroll: (event: Event) => {
          savedScroll = (event.target as HTMLElement).scrollTop;
        },
      },
      [m("div.claude-login-alts-list", getLanes().map(laneRow)), renderAccounts()],
    );
  }

  /** Already signed in, with re-auth on the row and a two-click remove. */
  function renderAccounts(): m.Children {
    const signedIn = getAccounts();
    if (signedIn.length === 0) return null;
    return m("div.claude-login-alts", [
      m("div.claude-login-section-label", "Signed in"),
      m(
        "div.claude-login-alts-list",
        signedIn.map((account) =>
          m("div.claude-login-account-row", { key: account.id }, [
            m(
              "button.claude-login-alt",
              {
                type: "button",
                title: "Sign in again, keeping this account",
                // Re-authenticating writes the SAME folder, so the account keeps its id and
                // every chat bound to it recovers. Minting a new one would leave them all
                // pointed at a credential that has expired.
                onclick: () => {
                  const owner = getLanes().find((candidate) => candidate.id === account.lane);
                  if (owner !== undefined) void begin(owner, owner.methods[0], account.id);
                },
              },
              [
                m("span.claude-login-alt-mark", m.trust(providerMark(account.lane, 22))),
                m("span.claude-login-alt-text", m("span.claude-login-alt-name", account.label)),
              ],
            ),
            m(
              "button.claude-login-account-remove",
              {
                type: "button",
                "aria-label": confirmingDelete === account.id ? "Confirm removal" : `Remove ${account.label}`,
                onclick: () => {
                  if (confirmingDelete !== account.id) {
                    confirmingDelete = account.id;
                    return;
                  }
                  confirmingDelete = null;
                  void send(() => deleteAccount(account.id));
                },
              },
              confirmingDelete === account.id ? "Remove?" : m.trust(icon("trash", { size: 15 })),
            ),
          ]),
        ),
      ),
      confirmingDelete !== null
        ? m("p.claude-login-copylink", "Chats already running on it will not be able to take another turn.")
        : null,
    ]);
  }

  /** Step 1 for both browser shapes: the link, with a copy fallback for when the
   *  workspace's browser handoff does not fire. */
  function renderOpenLinkStep(url: string, label: string): m.Children {
    return m("div.claude-login-step", { class: activeStep === 1 ? "" : "claude-login-step--done" }, [
      m("div.claude-login-step-label", [m("span.claude-login-step-num", "1"), "Open the sign-in page"]),
      m(
        "a.claude-login-button.claude-login-button--primary.claude-login-button--block.claude-login-button--link",
        {
          href: url,
          target: "_blank",
          rel: "noopener noreferrer",
          onclick: () => {
            activeStep = 2;
          },
        },
        [m("span", label), m.trust(icon("external-link", { size: 15 }))],
      ),
      m("p.claude-login-copylink", [
        "Didn't open? ",
        m(
          "button.claude-login-copylink-action",
          { type: "button", onclick: () => void copyToClipboard(url, "link") },
          copied === "link" ? "Link copied" : copyFailed ? "Failed to copy" : "Copy the link",
        ),
        copied === "link" ? "" : copyFailed ? " -- copy this link manually:" : " and paste it into your browser.",
      ]),
      // A rejected clipboard write leaves the raw link on screen, so there is always a way
      // to reach the sign-in page by hand.
      copyFailed ? m("div.claude-login-rawurl", { tabindex: 0 }, url) : null,
    ]);
  }

  /** Step 2, `url_then_code`: the browser hands back a code and we take it. */
  function renderPasteCodeStep(): m.Children {
    return m("div.claude-login-step", { class: activeStep === 2 ? "" : "claude-login-step--done" }, [
      m("div.claude-login-step-label", [m("span.claude-login-step-num", "2"), "Approve, then paste the code shown"]),
      m("div.claude-login-subtle-body", [
        m("input.claude-login-input.claude-login-input--mono", {
          type: "text",
          value: codeInput,
          placeholder: "authorization code",
          spellcheck: false,
          autocomplete: "off",
          onfocus: () => {
            activeStep = 2;
          },
          oninput: (event: Event) => {
            codeInput = (event.target as HTMLInputElement).value;
          },
          onkeydown: (event: KeyboardEvent) => {
            if (event.key === "Enter" && codeInput.trim() !== "") {
              event.preventDefault();
              void send(() => submitCode(codeInput.trim()));
            }
          },
        }),
        m(
          "button.claude-login-button.claude-login-button--primary",
          {
            type: "button",
            disabled: busy || codeInput.trim() === "",
            onclick: () => void send(() => submitCode(codeInput.trim())),
          },
          busy ? "Verifying..." : "Verify code",
        ),
      ]),
    ]);
  }

  /** Step 2, `code_then_wait`: the code is OURS to show, and it goes into the browser.
   *  Nothing comes back here, so there is no field -- we poll until the CLI says it is in,
   *  and the copy button is the whole interaction. */
  function renderShowCodeStep(code: string | null): m.Children {
    return m("div.claude-login-step", { class: activeStep === 2 ? "" : "claude-login-step--done" }, [
      m("div.claude-login-step-label", [m("span.claude-login-step-num", "2"), "Enter this code on that page"]),
      m("div.claude-login-code-row", [
        m("div.claude-login-code", code ?? ""),
        m(
          "button.claude-login-button.claude-login-button--ghost",
          {
            type: "button",
            disabled: code === null,
            onclick: () => void copyToClipboard(code ?? "", "code"),
          },
          copied === "code" ? "Copied" : copyFailed ? "Failed to copy" : "Copy code",
        ),
      ]),
      m("div.claude-login-applying", "Waiting for you to finish in the browser..."),
    ]);
  }

  /** The paste shape: no browser, no steps -- one field. */
  function renderPaste(current: Lane): m.Children {
    const choices = current.key_providers;
    const selected = choices.find((candidate) => candidate.provider_id === keyProvider) ?? null;
    return [
      m("p.claude-login-lead", `Paste a ${selected?.display ?? current.provider_name} API key.`),
      // Only worth a picker when there is a choice; single-provider lanes skip it.
      choices.length > 1
        ? m(
            "select.claude-login-input.claude-login-select",
            {
              value: keyProvider ?? "",
              onchange: (event: Event) => {
                keyProvider = (event.target as HTMLSelectElement).value;
              },
            },
            choices.map((candidate) =>
              m("option", { value: candidate.provider_id, key: candidate.provider_id }, candidate.display),
            ),
          )
        : null,
      m("div.claude-login-subtle-body", [
        m("input.claude-login-input.claude-login-input--mono", {
          type: "password",
          value: keyInput,
          placeholder: selected?.hint || "API key",
          spellcheck: false,
          autocomplete: "off",
          oninput: (event: Event) => {
            keyInput = (event.target as HTMLInputElement).value;
          },
          onkeydown: (event: KeyboardEvent) => {
            if (event.key === "Enter" && keyInput.trim() !== "") {
              event.preventDefault();
              void send(() => submitKey(keyInput.trim(), keyProvider));
            }
          },
        }),
        m(
          "button.claude-login-button.claude-login-button--primary",
          {
            type: "button",
            disabled: busy || keyInput.trim() === "",
            onclick: () => void send(() => submitKey(keyInput.trim(), keyProvider)),
          },
          busy ? "Saving..." : "Save key",
        ),
      ]),
      selected !== null && selected.env_var !== ""
        ? m("p.claude-login-copylink", `Saved as ${selected.env_var} for this mind.`)
        : null,
    ];
  }

  function renderSignIn(current: Lane): m.Children {
    const flow = getFlow();
    if (flow !== null && flow.status.state === "ok") {
      return m("div.claude-login-applying", [
        m.trust(icon("check", { size: 18 })),
        reauthAccountId === null
          ? ` Signed in to ${current.provider_name}.`
          : " Signed in again. Every chat on this provider can take a turn once more.",
      ]);
    }
    if (busy && flow === null) return m("div.claude-login-applying", "Starting sign-in...");
    if (flow === null) return null;
    if (flow.shape === "paste") return renderPaste(current);

    const lead =
      flow.shape === "code_then_wait"
        ? `Use your ${current.provider_name} account. Open the page below and enter the code we show you.`
        : `Use your ${current.provider_name} account. Approve access in your browser, copy the code it gives you, then paste it below.`;
    return [
      m("p.claude-login-lead", lead),
      flow.url !== null ? renderOpenLinkStep(flow.url, `Open ${current.provider_name} sign-in page`) : null,
      flow.shape === "code_then_wait" ? renderShowCodeStep(flow.code) : renderPasteCodeStep(),
    ];
  }

  /** The other ways into this lane, flat under a labelled section (the mockup drops the
   *  fold -- with two or three entries a disclosure hides more than it saves). */
  function renderAlternates(current: Lane): m.Children {
    const others = current.methods.filter((candidate) => candidate.id !== method?.id);
    if (others.length === 0) return null;
    const flow = getFlow();
    if (flow !== null && flow.status.state === "ok") return null;
    return m("div.claude-login-alts", [
      m("button.claude-login-alts-toggle", { type: "button", onclick: () => (showAlternates = !showAlternates) }, [
        m(
          "span.claude-login-alts-caret",
          { class: showAlternates ? "claude-login-alts-caret--open" : "" },
          m.trust(icon("chevron-right", { size: 14 })),
        ),
        `Other ways to sign in (${others.length})`,
      ]),
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
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) onClose();
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

/** Re-exported for the launcher's picker, which lists the same accounts. */
export type { ProviderAccount };
