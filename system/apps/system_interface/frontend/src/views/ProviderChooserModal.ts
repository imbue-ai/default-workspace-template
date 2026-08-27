/**
 * The provider chooser, and the sign-in screens behind it.
 *
 * The markup and classes are the mockup's (`prototypes/minds-harness`), ported verbatim --
 * see `providerSignInStyles.ts`, which holds the class strings as literals so a later diff
 * against the mockup still means something. Two levels: a scroll region of uniform rows
 * (mark, name, subtitle, chevron), and the sign-in you drill into, with a back chevron in
 * the header. The chooser's scroll offset survives a drill-in and back, so you land where
 * you were; the chooser unmounts while a sign-in is up, so that lives at module scope.
 *
 * Where we diverge from the mockup, we diverge only in the part that differs and keep its
 * frame -- see the plan's "UI comes from the mockup verbatim" addendum:
 *
 *   * no `Runs on <harness>` dropdown, and no `Runs on X` line on the rows: provider ->
 *     harness is fixed in V1, so neither is a choice or a fact worth a third line;
 *   * `code_then_wait` (OpenAI's device flow) runs the other direction -- it shows a code
 *     you carry to the browser rather than taking one back -- so its step 2 is a code plus
 *     a Copy button. The mockup has no screen for it; everything around it is still the
 *     mockup's numbered-step block;
 *   * signed-in accounts, re-auth and delete are ours: the mockup shows one connection.
 *
 * The three shapes come from the server per method, so nothing here knows what a harness is:
 *
 *   url_then_code   1. open the link  2. paste the code it gives you
 *   code_then_wait  1. open the link  2. enter the code WE show, and wait
 *   paste           no browser at all -- one field, paste a key
 */

import m from "mithril";

import { icon } from "./icons";
import { providerMark } from "./providerMarks";
import * as css from "./providerSignInStyles";
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

/** The chooser's last scroll offset, kept across a drill-in and back. The chooser's DOM
 *  unmounts while a sign-in is showing, so a component-level ref would not survive it --
 *  module scope is what makes it outlast the remount. (Same reasoning as the mockup's.) */
let savedScroll = 0;

export function ProviderChooserModal(): m.Component<ProviderChooserModalAttrs> {
  let lane: Lane | null = null;
  let method: LaneMethod | null = null;
  let codeInput = "";
  let keyInput = "";
  let keyProvider: string | null = null;
  let error: string | null = null;
  let busy = false;
  // The account a re-authentication is writing into. Keeping the folder is the point: every
  // chat bound to it by label comes back rather than being orphaned by an expiry.
  let reauthAccountId: string | null = null;
  let confirmingDelete: string | null = null;
  // Which numbered step is lit; the other desaturates. Advances when the link is opened.
  let activeStep: 1 | 2 = 1;
  let copied: "" | "link" | "code" = "";
  let copyFailed = false;

  function reset(): void {
    lane = null;
    method = null;
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
      // Insecure context or a denied permission. Reveal the raw value so the user is never
      // stranded without a way to reach the sign-in page by hand.
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

  function stepClass(step: 1 | 2): string {
    return `${css.STEP} ${css.STEP_TRANSITION} ${activeStep === step ? "" : css.STEP_DONE}`;
  }

  function stepLabel(num: string, text: string): m.Vnode {
    return m("div", { class: css.STEP_LABEL }, [m("span", { class: css.STEP_NUM }, num), text]);
  }

  /** IntroChooserModal's ChooserRow. */
  function laneRow(candidate: Lane): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        key: candidate.id,
        class: css.CHOOSER_ROW,
        onclick: () => begin(candidate, candidate.methods[0]),
      },
      [
        m("span", { class: css.CHOOSER_ROW_MARK }, m.trust(providerMark(candidate.id, 30))),
        m("span", { class: css.CHOOSER_ROW_TEXT }, [
          m("span", { class: css.CHOOSER_ROW_NAME }, candidate.provider_name),
          m("span", { class: css.CHOOSER_ROW_BODY }, candidate.subtitle),
        ]),
        m("span", { class: css.CHOOSER_ROW_CHEVRON }, m.trust(icon("chevron-right", { size: 18 }))),
      ],
    );
  }

  function renderChooser(): m.Children {
    if (!areLanesLoaded()) return m("div", { class: css.APPLYING }, "Loading providers...");
    return m(
      "div",
      {
        class: css.SCROLL,
        oncreate: (vnode: m.VnodeDOM) => {
          (vnode.dom as HTMLElement).scrollTop = savedScroll;
        },
        onscroll: (event: Event) => {
          savedScroll = (event.target as HTMLElement).scrollTop;
        },
      },
      [m("div", { class: css.ROW_STACK }, getLanes().map(laneRow)), renderAccounts()],
    );
  }

  /** Ours, not the mockup's: it shows one connection. Uses its OptionRow grammar. */
  function renderAccounts(): m.Children {
    const signedIn = getAccounts();
    if (signedIn.length === 0) return null;
    return m("div", { class: "mt-6" }, [
      m("div", { class: css.SECTION_LABEL }, "Signed in"),
      m(
        "div",
        { class: css.ROW_STACK },
        signedIn.map((account) =>
          m("div", { class: "flex items-stretch gap-1.5", key: account.id }, [
            m(
              "button",
              {
                type: "button",
                class: `${css.OPTION_ROW} min-w-0 flex-1`,
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
                m("span", { class: "flex min-w-0 items-center gap-2" }, [
                  m(
                    "span",
                    { class: "flex w-6 shrink-0 items-center justify-center" },
                    m.trust(providerMark(account.lane, 20)),
                  ),
                  m("span", { class: `${css.OPTION_ROW_NAME} truncate` }, account.label),
                ]),
              ],
            ),
            m(
              "button",
              {
                type: "button",
                class: `${css.HEADER_BUTTON} h-auto w-auto px-2 text-[0.75rem]`,
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
        ? m("p", { class: css.HINT }, "Chats already running on it will not be able to take another turn.")
        : null,
    ]);
  }

  /** Step 1 of the mockup's stepsBlock, plus the copy-link fallback the old modal had. */
  function renderOpenLinkStep(url: string, label: string): m.Children {
    return m("div", { class: stepClass(1) }, [
      stepLabel("1", "Open the sign-in page"),
      m(
        "a",
        {
          class: css.PRIMARY_LINK_BTN,
          href: url,
          target: "_blank",
          rel: "noopener noreferrer",
          onclick: () => {
            activeStep = 2;
          },
        },
        [m("span", label), m.trust(icon("external-link", { size: 15 }))],
      ),
      m("p", { class: css.HINT }, [
        "Didn't open? ",
        m(
          "button",
          { type: "button", class: css.HINT_ACTION, onclick: () => void copyToClipboard(url, "link") },
          copied === "link" ? "Link copied" : copyFailed ? "Failed to copy" : "Copy the link",
        ),
        copied === "link" ? "" : copyFailed ? " -- copy this link manually:" : " and paste it into your browser.",
      ]),
      copyFailed ? m("div", { class: css.RAW_VALUE, tabindex: 0 }, url) : null,
    ]);
  }

  /** Step 2 of the mockup's stepsBlock. */
  function renderPasteCodeStep(): m.Children {
    return m("div", { class: stepClass(2) }, [
      stepLabel("2", "Approve, then paste the code shown"),
      m("div", { class: css.FIELD_ROW }, [
        m("input", {
          class: `${css.INPUT} flex-1`,
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
          "button",
          {
            type: "button",
            class: css.PRIMARY_BTN,
            disabled: busy || codeInput.trim() === "",
            onclick: () => void send(() => submitCode(codeInput.trim())),
          },
          busy ? "Verifying..." : "Verify code",
        ),
      ]),
    ]);
  }

  /** Ours: the inverted flow's step 2. The code is ours to show and it goes into the
   *  browser, so there is no field -- we poll, and the Copy button is the interaction. */
  function renderShowCodeStep(code: string | null): m.Children {
    return m("div", { class: stepClass(2) }, [
      stepLabel("2", "Enter this code on that page"),
      m("div", { class: css.FIELD_ROW }, [
        m("div", { class: css.CODE }, code ?? ""),
        m(
          "button",
          {
            type: "button",
            class: css.SECONDARY_BTN,
            disabled: code === null,
            onclick: () => void copyToClipboard(code ?? "", "code"),
          },
          copied === "code" ? "Copied" : copyFailed ? "Failed to copy" : "Copy code",
        ),
      ]),
      m("div", { class: css.APPLYING }, "Waiting for you to finish in the browser..."),
    ]);
  }

  /** The mockup's apiKey screen: no browser, no steps -- one field. */
  function renderPaste(current: Lane): m.Children {
    const choices = current.key_providers;
    const selected = choices.find((candidate) => candidate.provider_id === keyProvider) ?? null;
    return [
      m("p", { class: css.LEAD }, `Paste a ${selected?.display ?? current.provider_name} API key.`),
      choices.length > 1
        ? m(
            "select",
            {
              class: `${css.INPUT} mb-2`,
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
      m("div", { class: css.FIELD_ROW }, [
        m("input", {
          class: `${css.INPUT} flex-1`,
          type: "password",
          value: keyInput,
          placeholder: selected?.hint || "Paste your key",
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
          "button",
          {
            type: "button",
            class: css.PRIMARY_BTN,
            disabled: busy || keyInput.trim() === "",
            onclick: () => void send(() => submitKey(keyInput.trim(), keyProvider)),
          },
          busy ? "Saving..." : "Connect",
        ),
      ]),
      selected !== null && selected.env_var !== ""
        ? m("p", { class: css.HINT }, `Saved as ${selected.env_var} for this mind.`)
        : null,
    ];
  }

  function renderSignIn(current: Lane): m.Children {
    const flow = getFlow();
    if (flow !== null && flow.status.state === "ok") {
      return m("div", { class: css.APPLYING }, [
        m.trust(icon("check", { size: 18 })),
        reauthAccountId === null
          ? `Signed in to ${current.provider_name}.`
          : "Signed in again. Every chat on this provider can take a turn once more.",
      ]);
    }
    if (busy && flow === null) return m("div", { class: css.APPLYING }, "Starting sign-in...");
    if (flow === null) return null;
    if (flow.shape === "paste") return renderPaste(current);

    const lead =
      flow.shape === "code_then_wait"
        ? `Use your ${current.provider_name} account. Open the page below and enter the code we show you.`
        : `Use your ${current.provider_name} account. Approve access in your browser, copy the code it gives you, then paste it below.`;
    return [
      m("p", { class: css.LEAD }, lead),
      flow.url !== null ? renderOpenLinkStep(flow.url, `Open ${current.provider_name} sign-in page`) : null,
      flow.shape === "code_then_wait" ? renderShowCodeStep(flow.code) : renderPasteCodeStep(),
    ];
  }

  /** The mockup's "Other ways to sign in": a labelled section of OptionRows, flat -- no
   *  fold and no divider. */
  function renderAlternates(current: Lane): m.Children {
    const others = current.methods.filter((candidate) => candidate.id !== method?.id);
    const flow = getFlow();
    if (others.length === 0 || flow?.status.state === "ok") return null;
    return m("div", { class: "mt-6" }, [
      m("div", { class: css.SECTION_LABEL }, "Other ways to sign in"),
      m(
        "div",
        { class: css.ROW_STACK },
        others.map((candidate) =>
          m(
            "button",
            {
              type: "button",
              key: candidate.id,
              class: css.OPTION_ROW,
              onclick: () => begin(current, candidate),
            },
            [
              m("span", { class: "flex min-w-0 flex-col" }, [
                m("span", { class: css.OPTION_ROW_NAME }, candidate.label),
                m("span", { class: css.OPTION_ROW_DESC }, candidate.description),
              ]),
              m("span", { class: css.CHOOSER_ROW_CHEVRON }, m.trust(icon("chevron-right", { size: 18 }))),
            ],
          ),
        ),
      ),
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
        m(
          "div",
          { class: css.MODAL, role: "dialog", "aria-modal": "true", "aria-label": "Pick your AI provider" },
          m("div", { class: css.PANEL }, [
            m("div", { class: css.HEADER }, [
              lane !== null
                ? m(
                    "button",
                    { type: "button", class: `-ml-1 ${css.HEADER_BUTTON}`, onclick: back, "aria-label": "Back" },
                    m.trust(icon("chevron-left", { size: 16 })),
                  )
                : null,
              m("h2", { class: css.TITLE }, lane === null ? "Pick your AI provider" : lane.provider_name),
              m(
                "button",
                { type: "button", class: css.CLOSE_BUTTON, onclick: onClose, "aria-label": "Close" },
                m.trust(icon("close", { size: 16 })),
              ),
            ]),
            lane === null
              ? renderChooser()
              : m("div", { class: css.BODY }, [
                  error !== null || detail !== null ? m("div", { class: css.NOTICE }, error ?? detail) : null,
                  renderSignIn(lane),
                  renderAlternates(lane),
                ]),
          ]),
        ),
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
