/**
 * The provider chooser and its sign-in screens, ported from the mockup
 * (`imbue-ai/mind-sketches`, `prototypes/minds-harness`).
 *
 * It is the mockup's two dialogs collapsed into one component, because our flow has no
 * router: `IntroChooserModal`'s row list is the entry screen, and picking a row swaps the
 * same panel to `ProviderSignInModal`'s body with a back chevron in the header. The class
 * strings all come from `providerSignInStyles.ts`, verbatim.
 *
 * The mode names are the mockup's, and so is what each renders:
 *
 *   chooser    the lane rows (IntroChooserModal)
 *   menu       a lane's primary method, plus "Other ways to sign in" under it
 *   steps      one browser method, as the numbered 1-2 sequence
 *   apiKey     one paste method: pick a provider, paste the key
 *   verifying  the spinner screen
 *   success    the check screen, with a Done footer
 *
 * Where we diverge from the mockup, only the part that differs changes and the frame
 * around it stays (see the plan's "UI comes from the mockup verbatim" addendum):
 *
 *   * no `Runs on <harness>` dropdown, and no `Runs on X` line on the rows -- provider ->
 *     harness is fixed in V1, so neither is a choice or a fact worth a third line;
 *   * `code_then_wait` (OpenAI's device flow) runs the other way round: it shows a code you
 *     carry to the browser rather than taking one back, so its step 2 is a code plus a Copy
 *     button. The mockup has no screen for it; it still sits in the numbered-step block;
 *   * the signed-in list, re-auth and delete are ours -- the mockup shows one connection.
 *
 * Which of the three shapes a method uses comes from the server, so nothing here knows
 * what a harness is.
 */

import m from "mithril";

import { icon, loginSpinnerIcon, warningIcon } from "./icons";
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

type Mode = "chooser" | "menu" | "steps" | "apiKey";

/** The chooser's last scroll offset, so a drill-in and back lands where you were. The
 *  chooser's DOM unmounts while a sign-in is up, so this outlives it at module scope --
 *  the same reason the mockup keeps it there. */
let savedScroll = 0;

export function ProviderChooserModal(): m.Component<ProviderChooserModalAttrs> {
  let mode: Mode = "chooser";
  let lane: Lane | null = null;
  let method: LaneMethod | null = null;
  /** True when this screen was entered straight from the chooser, so back returns there
   *  rather than to a menu that was never shown. The mockup's `cameFromChooser`. */
  let cameFromChooser = false;
  let codeInput = "";
  let keyInput = "";
  let keyProvider: string | null = null;
  let error: string | null = null;
  let busy = false;
  let reauthAccountId: string | null = null;
  let confirmingDelete: string | null = null;
  let activeStep: 1 | 2 = 1;
  let copied: "" | "link" | "code" = "";
  let copyFailed = false;
  // Set once a credential has been handed over and we are waiting on the verdict. The
  // request itself returns long before the answer does -- the server hands the code to the
  // CLI and the harness's own probe decides, which the client learns from a later poll --
  // so `busy` alone leaves a gap where the flow is still pending and nothing marks it. That
  // gap rendered the menu again, which read as being bounced back to the start.
  let awaitingVerdict = false;

  function reset(): void {
    mode = "chooser";
    lane = null;
    method = null;
    cameFromChooser = false;
    codeInput = "";
    keyInput = "";
    keyProvider = null;
    error = null;
    reauthAccountId = null;
    confirmingDelete = null;
    activeStep = 1;
    copied = "";
    copyFailed = false;
    awaitingVerdict = false;
    clearFlow();
  }

  function isPaste(candidate: LaneMethod): boolean {
    return candidate.shape === "paste";
  }

  async function copyToClipboard(value: string, kind: "link" | "code"): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      copied = kind;
      copyFailed = false;
    } catch {
      // Insecure context or a denied permission -- reveal the raw value instead, so the
      // user is never left without a way to reach the page by hand.
      copied = "";
      copyFailed = true;
    }
    m.redraw();
  }

  /** Open a lane's method. `fromChooser` decides where back goes. */
  async function begin(
    chosen: Lane,
    chosenMethod: LaneMethod,
    options: { fromChooser: boolean; accountId?: string },
  ): Promise<void> {
    lane = chosen;
    method = chosenMethod;
    cameFromChooser = options.fromChooser;
    mode = isPaste(chosenMethod) ? "apiKey" : chosen.methods.length > 1 && options.fromChooser ? "menu" : "steps";
    error = null;
    activeStep = 1;
    copied = "";
    copyFailed = false;
    codeInput = "";
    keyInput = "";
    awaitingVerdict = false;
    reauthAccountId = options.accountId ?? null;
    keyProvider = chosen.key_providers.length === 1 ? chosen.key_providers[0].provider_id : null;
    busy = true;
    m.redraw();
    try {
      await startFlow(chosen.id, chosenMethod.id, options.accountId);
    } catch (e) {
      error = errorText(e);
    } finally {
      busy = false;
      m.redraw();
    }
  }

  /** Run a credential handover. `holdUntilVerdict` keeps the waiting screen up after the
   *  request returns, for the flows whose answer arrives on a later poll rather than in the
   *  response body. */
  async function send(action: () => Promise<void>, holdUntilVerdict = false): Promise<void> {
    busy = true;
    error = null;
    if (holdUntilVerdict) awaitingVerdict = true;
    m.redraw();
    try {
      await action();
    } catch (e) {
      error = errorText(e);
      awaitingVerdict = false;
    } finally {
      busy = false;
      m.redraw();
    }
  }

  /** The mockup's back: a deep screen returns to the menu, an entry screen to the chooser. */
  function back(): void {
    abortFlow();
    if (mode === "menu" || cameFromChooser || lane === null) {
      reset();
      m.redraw();
      return;
    }
    const owner = lane;
    const primary = owner.methods[0];
    void begin(owner, primary, { fromChooser: true });
  }

  function stepBlock(step: 1 | 2, isLast: boolean, children: m.Children): m.Vnode {
    const base = isLast ? css.STEP_LAST : css.STEP;
    return m("div", { class: `${base} ${activeStep === step ? "" : css.STEP_DIMMED}` }, children);
  }

  function stepLabel(num: string | null, text: string): m.Vnode {
    return m("div", { class: css.STEP_LABEL }, [num !== null ? m("span", { class: css.STEP_NUM }, num) : null, text]);
  }

  function statusScreen(
    kind: "pending" | "success" | "error",
    title: string,
    detail: string | null,
    mark: m.Children = null,
  ): m.Vnode {
    const glyph =
      kind === "pending"
        ? loginSpinnerIcon()
        : kind === "success"
          ? icon("check", { size: 26, strokeWidth: 2.5 })
          : warningIcon();
    const disc =
      kind === "pending"
        ? css.STATUS_DISC_PENDING
        : kind === "success"
          ? css.STATUS_DISC_SUCCESS
          : css.STATUS_DISC_ERROR;
    return m("div", { class: css.STATUS }, [
      m("div", { class: disc }, m.trust(glyph)),
      m("h3", { class: css.STATUS_TITLE }, title),
      detail !== null ? m("p", { class: css.STATUS_DETAIL }, detail) : null,
      mark,
    ]);
  }

  // --- chooser ---------------------------------------------------------------------------

  /** IntroChooserModal's ChooserRow. */
  function laneRow(candidate: Lane): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        key: candidate.id,
        class: css.CHOOSER_ROW,
        onclick: () => begin(candidate, candidate.methods[0], { fromChooser: true }),
      },
      [
        m("span", { class: css.CHOOSER_ROW_MARK }, m.trust(providerMark(candidate.id, 30))),
        m("span", { class: css.CHOOSER_ROW_TEXT }, [
          m("span", { class: css.CHOOSER_ROW_NAME }, candidate.provider_name),
          m("span", { class: css.CHOOSER_ROW_BODY }, candidate.subtitle),
        ]),
        m("span", { class: css.CHEVRON }, m.trust(icon("chevron-right", { size: 18 }))),
      ],
    );
  }

  function renderChooser(): m.Children {
    if (!areLanesLoaded()) return statusScreen("pending", "Loading providers...", null);
    return [m("div", { class: css.ROW_STACK }, getLanes().map(laneRow)), renderAccounts()];
  }

  /** Ours: the mockup shows one connection. A signed-in account is a STATE, not a place to
   *  navigate to, so the row is not a button -- it reads as a listed fact with two explicit
   *  actions beside it. Re-auth stays reachable because an expired credential is otherwise
   *  a dead end: without it the only way back is to delete the account, which orphans every
   *  chat bound to it rather than reviving them. */
  function renderAccounts(): m.Children {
    const signedIn = getAccounts();
    if (signedIn.length === 0) return null;
    return m("div", { class: "mt-6" }, [
      m("div", { class: css.SECTION_LABEL }, "Signed in"),
      m(
        "div",
        { class: css.ROW_STACK },
        signedIn.map((account) =>
          m("div", { class: css.ACCOUNT_ROW, key: account.id }, [
            m(
              "span",
              { class: "flex w-6 shrink-0 items-center justify-center" },
              m.trust(providerMark(account.lane, 20)),
            ),
            m("span", { class: `${css.OPTION_ROW_NAME} min-w-0 flex-1 truncate` }, account.label),
            m(
              "button",
              {
                type: "button",
                class: css.ROW_ACTION,
                title: "Sign in again, keeping this account and every chat on it",
                onclick: () => {
                  const owner = getLanes().find((candidate) => candidate.id === account.lane);
                  if (owner !== undefined) {
                    void begin(owner, owner.methods[0], { fromChooser: true, accountId: account.id });
                  }
                },
              },
              "Sign in again",
            ),
            m(
              "button",
              {
                type: "button",
                class: css.ROW_ACTION,
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

  // --- sign-in bodies --------------------------------------------------------------------

  /** ProviderSignInModal's stepsBlock, step 1, plus the old modal's copy-link fallback. */
  function openLinkStep(url: string, label: string): m.Vnode {
    return stepBlock(1, false, [
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
        copied === "link" ? "" : copyFailed ? " -- copy it manually:" : " and paste it into your browser.",
      ]),
      copyFailed ? m("div", { class: css.RAW_VALUE, tabindex: 0 }, url) : null,
    ]);
  }

  /** stepsBlock, step 2. */
  function pasteCodeStep(): m.Vnode {
    return stepBlock(2, true, [
      stepLabel("2", "Approve, then paste the code shown"),
      m("div", { class: css.FIELD_ROW }, [
        m("input", {
          class: `${css.INPUT} flex-1`,
          type: "text",
          value: codeInput,
          placeholder: "CODE#STATE",
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
              void send(() => submitCode(codeInput.trim()), true);
            }
          },
        }),
        m(
          "button",
          {
            type: "button",
            class: `${css.PRIMARY_BTN} whitespace-nowrap`,
            disabled: busy || codeInput.trim() === "",
            onclick: () => void send(() => submitCode(codeInput.trim()), true),
          },
          "Verify code",
        ),
      ]),
    ]);
  }

  /** Ours: the device flow's step 2. The code is ours to show and it goes INTO the
   *  browser, so there is no field -- we poll, and Copy is the whole interaction. */
  function showCodeStep(code: string | null): m.Vnode {
    return stepBlock(2, true, [
      stepLabel("2", "Enter this code on that page"),
      m("div", { class: css.FIELD_ROW }, [
        m("div", { class: css.CODE }, code ?? ""),
        m(
          "button",
          {
            type: "button",
            class: `${css.SECONDARY_BTN} whitespace-nowrap`,
            disabled: code === null,
            onclick: () => void copyToClipboard(code ?? "", "code"),
          },
          copied === "code" ? "Copied" : copyFailed ? "Failed to copy" : "Copy code",
        ),
      ]),
      m("p", { class: css.HINT }, "Waiting for you to finish in the browser..."),
    ]);
  }

  /** The browser sequence for one method. */
  function stepsBody(current: Lane, currentMethod: LaneMethod): m.Children {
    const flow = getFlow();
    if (flow === null) return null;
    const lead =
      flow.shape === "code_then_wait"
        ? `Use your ${current.provider_name} account. Open the page below and enter the code we show you.`
        : `Use your ${current.provider_name} account. Approve access in your browser, copy the code it gives you, then paste it below.`;
    return [
      m("p", { class: css.LEAD }, currentMethod.description || lead),
      flow.url !== null ? openLinkStep(flow.url, `Open ${current.provider_name} sign-in page`) : null,
      flow.shape === "code_then_wait" ? showCodeStep(flow.code) : pasteCodeStep(),
    ];
  }

  /** ProviderSignInModal's apiKey case. With one provider it is just the field; with a
   *  list it is the two-step pick-then-paste, which is the interesting one. */
  function apiKeyBody(current: Lane): m.Children {
    const choices = current.key_providers;
    const selected = choices.find((candidate) => candidate.provider_id === keyProvider) ?? null;
    const withPicker = choices.length > 1;
    const keyField = m("div", { class: css.FIELD_ROW }, [
      m("input", {
        class: `${css.INPUT} flex-1`,
        type: "password",
        value: keyInput,
        placeholder: withPicker ? (selected?.hint ?? "Paste your key") : (selected?.hint ?? "sk-..."),
        spellcheck: false,
        autocomplete: "off",
        "data-1p-ignore": "",
        oninput: (event: Event) => {
          keyInput = (event.target as HTMLInputElement).value;
        },
        onkeydown: (event: KeyboardEvent) => {
          if (event.key === "Enter" && keyInput.trim() !== "" && (!withPicker || keyProvider !== null)) {
            event.preventDefault();
            void send(() => submitKey(keyInput.trim(), keyProvider));
          }
        },
      }),
      m(
        "button",
        {
          type: "button",
          class: `${css.PRIMARY_BTN} whitespace-nowrap`,
          disabled: busy || keyInput.trim() === "" || (withPicker && keyProvider === null),
          onclick: () => void send(() => submitKey(keyInput.trim(), keyProvider)),
        },
        "Save & finish",
      ),
    ]);

    if (!withPicker) {
      return m("div", [
        m("div", { class: css.SECTION_LABEL }, "Use an API key"),
        m("p", { class: css.LEAD }, method?.description ?? `Paste a ${current.provider_name} API key.`),
        stepLabel(null, "Your API key"),
        keyField,
        selected !== null && selected.env_var !== ""
          ? m("p", { class: css.HINT }, `Saved as ${selected.env_var} for this mind.`)
          : null,
      ]);
    }
    return m("div", [
      m("p", { class: css.LEAD }, method?.description ?? "Pick the provider, then paste its key."),
      stepBlock(1, false, [
        stepLabel("1", "Pick your provider"),
        m(
          "select",
          {
            class: css.INPUT,
            value: keyProvider ?? "",
            onchange: (event: Event) => {
              keyProvider = (event.target as HTMLSelectElement).value || null;
              activeStep = 2;
            },
          },
          [
            m("option", { value: "", disabled: true }, "Choose a provider"),
            ...choices.map((candidate) =>
              m("option", { value: candidate.provider_id, key: candidate.provider_id }, candidate.display),
            ),
          ],
        ),
      ]),
      stepBlock(2, true, [
        stepLabel("2", "Paste your API key"),
        keyField,
        m(
          "p",
          { class: css.HINT },
          selected !== null && selected.env_var !== ""
            ? `Saved as ${selected.env_var} for this mind.`
            : "Pick a provider to see where the key is saved.",
        ),
      ]),
    ]);
  }

  /** The menu: this lane's primary method inline, with its alternates under it. The
   *  mockup's codex menu, which is why the section labels read the way they do. */
  function menuBody(current: Lane): m.Children {
    const primary = current.methods[0];
    const others = current.methods.slice(1);
    return m("div", { class: "flex flex-col" }, [
      m("div", { class: css.SECTION_LABEL }, isPaste(primary) ? "Use an API key" : "Use your subscription"),
      isPaste(primary) ? apiKeyBody(current) : stepsBody(current, primary),
      others.length > 0
        ? m("div", { class: "mt-6" }, [
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
                    onclick: () => begin(current, candidate, { fromChooser: false }),
                  },
                  [
                    m("span", { class: "flex min-w-0 flex-col" }, [
                      m("span", { class: css.OPTION_ROW_NAME }, candidate.label),
                      m("span", { class: css.OPTION_ROW_DESC }, candidate.description),
                    ]),
                    m("span", { class: css.CHEVRON }, m.trust(icon("chevron-right", { size: 18 }))),
                  ],
                ),
              ),
            ),
          ])
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
      const current = lane;
      const isSuccess = flow !== null && flow.status.state === "ok";
      const isFailed = flow !== null && flow.status.state === "failed";
      // Spawning the CLI and scraping its first screen takes seconds, and so does checking
      // a submitted credential -- long enough that an empty panel reads as a missed click.
      // A resolved flow wins: `isSuccess` / `isFailed` are tested before this, so holding
      // for the verdict cannot outlive the verdict.
      const isPending = current !== null && (busy || awaitingVerdict);

      const title = isSuccess
        ? "Signed in"
        : current === null
          ? "Pick your AI provider"
          : mode === "apiKey" && current.key_providers.length > 1
            ? "Sign in with API key"
            : `Sign in to ${current.provider_name}`;

      let body: m.Children;
      if (current === null) {
        body = renderChooser();
      } else if (isSuccess) {
        body = statusScreen(
          "success",
          "All set",
          reauthAccountId === null
            ? `Signed in. ${current.provider_name} is ready to use.`
            : "Signed in again. Every chat on this provider can take a turn once more.",
          m("div", { class: css.STATUS_MARK }, [m.trust(providerMark(current.id, 18)), current.provider_name]),
        );
      } else if (isFailed || error !== null) {
        body = [
          statusScreen("error", "That didn't work", (isFailed ? flow.status.detail : null) ?? error),
          m(
            "div",
            { class: css.FOOTER_ROW },
            m(
              "button",
              {
                type: "button",
                class: css.PRIMARY_BTN,
                onclick: () =>
                  void begin(current, method ?? current.methods[0], {
                    fromChooser: cameFromChooser,
                    accountId: reauthAccountId ?? undefined,
                  }),
              },
              "Try again",
            ),
          ),
        ];
      } else if (isPending) {
        body = statusScreen(
          "pending",
          "Signing in...",
          awaitingVerdict ? "Checking your code with the provider." : "Preparing your sign-in.",
        );
      } else if (mode === "apiKey") {
        body = apiKeyBody(current);
      } else if (mode === "menu") {
        body = menuBody(current);
      } else {
        body = method === null ? null : stepsBody(current, method);
      }

      // An entry screen is a fixed-height scroll region so the panel does not jump when you
      // drill in; the short deep forms flex. The mockup's rule, verbatim.
      const isEntryScreen = current === null || mode === "menu";
      const bodyClass = isEntryScreen && !isSuccess && !isPending ? css.BODY_SCROLLING : css.BODY_FLEXING;

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
              current !== null
                ? m(
                    "button",
                    { type: "button", class: css.BACK_BUTTON, onclick: back, "aria-label": "Back" },
                    m.trust(icon("chevron-left", { size: 16 })),
                  )
                : null,
              m("h2", { class: css.TITLE }, title),
              m(
                "button",
                { type: "button", class: css.CLOSE_BUTTON, onclick: onClose, "aria-label": "Close" },
                m.trust(icon("close", { size: 16 })),
              ),
            ]),
            m(
              "div",
              {
                class: bodyClass,
                oncreate: (node: m.VnodeDOM) => {
                  if (current === null) (node.dom as HTMLElement).scrollTop = savedScroll;
                },
                onscroll: (event: Event) => {
                  if (current === null) savedScroll = (event.target as HTMLElement).scrollTop;
                },
              },
              body,
            ),
            isSuccess
              ? m(
                  "div",
                  { class: css.FOOTER },
                  m(
                    "div",
                    { class: css.FOOTER_ROW },
                    m("button", { type: "button", class: css.PRIMARY_BTN, onclick: onClose }, "Done"),
                  ),
                )
              : null,
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
