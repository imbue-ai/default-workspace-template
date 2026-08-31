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
import { backdropDismissAttrs } from "./modalBackdrop";
import { providerMark } from "./providerMarks";
import { removeAccountDialog } from "./removeAccountDialog";
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
  takeChooserAccountId,
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
  // The provider dropdown's open state and where to pin it. It is rendered into the overlay
  // rather than inline because the panel is overflow-hidden -- an in-panel popover of 28
  // rows would simply be clipped. Same reason the mockup portals it to <body>.
  let keyMenuOpen = false;
  let keyMenuAnchor: DOMRect | null = null;
  // Set once a credential has been handed over and we are waiting on the verdict. The
  // request itself returns long before the answer does -- the server hands the code to the
  // CLI and the harness's own probe decides, which the client learns from a later poll --
  // so `busy` alone leaves a gap where the flow is still pending and nothing marks it. That
  // gap rendered the menu again, which read as being bounced back to the start.
  let awaitingVerdict = false;
  // Bumped by every `begin`, so a request that has been superseded can tell and stand down.
  let generation = 0;

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
    keyMenuOpen = false;
    keyMenuAnchor = null;
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
    // A terminal lane spends seconds spawning a CLI and scraping its first screen, so it
    // gets the waiting screen. A paste lane has nothing to wait for -- minting a folder
    // takes milliseconds -- so showing one would be a flash of a spinner between the click
    // and a form that was always going to be there. Render the form and let the mint land
    // behind it; the Save button needs the flow, and it cannot be clicked that fast.
    busy = !isPaste(chosenMethod);
    // Which attempt this is. Two lanes clicked in quick succession both run this function, and
    // the second stamps the screen before the first's request resolves -- so without a
    // generation check the loser's `catch` writes "that didn't work" over the winner's screen,
    // and its `finally` clears a `busy` the winner is still relying on.
    generation += 1;
    const attempt = generation;
    m.redraw();
    try {
      await startFlow(chosen.id, chosenMethod.id, options.accountId);
    } catch (e) {
      if (attempt !== generation) return;
      error = errorText(e);
    } finally {
      if (attempt === generation) {
        busy = false;
        m.redraw();
      }
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
    // Carry the account through. Backing out of an alternate method is still that account's
    // re-auth: dropping the id here silently turns it into a MINT, so the user finishes the
    // sign-in, gets a second row for the same provider, and the account they were trying to
    // revive is still dead along with every chat bound to it.
    void begin(owner, primary, { fromChooser: true, accountId: reauthAccountId ?? undefined });
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
    return m("div", { class: css.STATUS, "data-e2e": `status-${kind}` }, [
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
        // Hooks for the cross-repo Electron e2e (mngr-internal's `test_snapshot_resume.py`).
        // Attributes rather than copy or tailwind classes: it drives this dialog from another
        // repo, so anything it keys on must survive a wording change or a re-port.
        "data-e2e": `lane-${candidate.id}`,
        onclick: () => begin(candidate, candidate.methods[0], { fromChooser: true }),
      },
      [
        m("span", { class: css.CHOOSER_ROW_MARK }, m.trust(providerMark(candidate.id, 30))),
        m("span", { class: css.CHOOSER_ROW_TEXT }, [
          m("span", { class: css.CHOOSER_ROW_NAME }, candidate.provider_name),
          // A lane with nothing non-obvious to say has no subtitle, and gets no empty line
          // where one would be -- the row simply sits shorter than its neighbours.
          candidate.subtitle === "" ? null : m("span", { class: css.CHOOSER_ROW_BODY }, candidate.subtitle),
        ]),
        m("span", { class: css.CHEVRON }, m.trust(icon("chevron-right", { size: 18 }))),
      ],
    );
  }

  /** Fetch the lanes and the accounts. Also the retry after either fails. */
  async function load(): Promise<void> {
    busy = true;
    error = null;
    try {
      await loadLanes();
      await loadAccounts();
    } catch (e) {
      error = (e as Error).message || "Could not reach the workspace.";
    } finally {
      busy = false;
      m.redraw();
    }
  }

  function renderChooser(): m.Children {
    // A failed load leaves no lanes and no way to ask again, so the spinner is forever.
    if (error !== null && !areLanesLoaded()) {
      return [
        statusScreen("error", "Could not load providers", error),
        m(
          "div",
          { class: css.FOOTER_ROW },
          m(
            "button",
            { type: "button", class: css.PRIMARY_BTN, disabled: busy, onclick: () => void load() },
            "Try again",
          ),
        ),
      ];
    }
    if (!areLanesLoaded()) return statusScreen("pending", "Loading providers...", null);
    // Signed-in accounts lead: what you already have is the answer to "can I chat?", so it
    // must not hide below the fold of a long provider list. Both section headers exist only
    // once there IS a signed-in section -- a first-run user sees the bare provider list,
    // with nothing to explain.
    const signedIn = renderAccounts();
    return [
      signedIn,
      // Removing an account can fail -- a row with no folder, a store that will not write --
      // and without this the row simply stays put with no explanation.
      error !== null ? m("p", { class: `${css.HINT} text-red-600` }, error) : null,
      signedIn !== null ? m("div", { class: `${css.SECTION_LABEL} mt-6` }, "Add more") : null,
      m("div", { class: css.ROW_STACK }, getLanes().map(laneRow)),
    ];
  }

  /** Ours: the mockup shows one connection. A signed-in account is a STATE, not a place to
   *  navigate to, so the row is not a button -- it reads as a listed fact with two explicit
   *  actions beside it. Re-auth stays reachable because an expired credential is otherwise
   *  a dead end: without it the only way back is to delete the account, which orphans every
   *  chat bound to it rather than reviving them. */
  function renderAccounts(): m.Children {
    const signedIn = getAccounts();
    if (signedIn.length === 0) return null;
    const confirming = signedIn.find((account) => account.id === confirmingDelete) ?? null;
    return m("div", [
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
                onclick: () => void reauthenticate(account.id, account.lane),
              },
              "Sign in again",
            ),
            m(
              "button",
              {
                type: "button",
                class: css.ROW_ACTION,
                "aria-label": `Remove ${account.label}`,
                onclick: () => {
                  confirmingDelete = account.id;
                },
              },
              m.trust(icon("trash", { size: 15 })),
            ),
          ]),
        ),
      ),
      confirming !== null
        ? removeAccountDialog(
            confirming,
            () => {
              confirmingDelete = null;
              void send(() => deleteAccount(confirming.id));
            },
            () => {
              confirmingDelete = null;
            },
          )
        : null,
    ]);
  }

  // --- sign-in bodies --------------------------------------------------------------------

  /** ProviderSignInModal's stepsBlock, step 1, plus the old modal's copy-link fallback. */
  function openLinkStep(url: string, label: string, title = "Open the sign-in page"): m.Vnode {
    return stepBlock(1, false, [
      stepLabel("1", title),
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
          // No placeholder. It read as an instruction about the code's SHAPE, and the shape
          // differs per lane -- Google's is nothing like `CODE#STATE`. The step label already
          // says what to paste, so an example that is wrong half the time is worse than none.
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
  /** The dropdown itself, pinned under its trigger. Rendered by the overlay rather than the
   *  panel, so the panel's `overflow-hidden` cannot clip a 28-row list. */
  function keyProviderMenu(current: Lane): m.Children {
    if (!keyMenuOpen || keyMenuAnchor === null) return null;
    const anchor = keyMenuAnchor;
    return [
      m("button", {
        type: "button",
        class: css.PICKER_BACKDROP,
        "aria-label": "Close provider menu",
        // The shared helper, as the modal's own backdrop uses. It keys on mouse DOWN because a
        // click fires wherever the press ENDED: selecting text inside the menu and releasing
        // past its edge would otherwise read as "dismiss".
        ...backdropDismissAttrs(() => {
          keyMenuOpen = false;
        }),
      }),
      m(
        "div",
        {
          class: css.PICKER_MENU,
          style: `left: ${anchor.left}px; top: ${anchor.bottom + 6}px; width: ${anchor.width}px;`,
          onclick: (event: MouseEvent) => event.stopPropagation(),
        },
        current.key_providers.map((candidate) => {
          const active = candidate.provider_id === keyProvider;
          return m(
            "button",
            {
              type: "button",
              key: candidate.provider_id,
              class: `${css.PICKER_OPTION} ${active ? css.PICKER_OPTION_ACTIVE : css.PICKER_OPTION_IDLE}`,
              onclick: () => {
                keyProvider = candidate.provider_id;
                keyMenuOpen = false;
                activeStep = 2;
              },
            },
            [
              m("span", { class: active ? css.PICKER_OPTION_NAME_ACTIVE : css.PICKER_OPTION_NAME }, candidate.display),
              active ? m.trust(icon("check", { size: 15, strokeWidth: 2.5 })) : null,
            ],
          );
        }),
      ),
    ];
  }

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
        "data-e2e": "api-key-input",
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
          // `getFlow() === null` too: a paste screen renders BEFORE its flow exists -- there is
          // nothing to wait for, so `begin` shows the form and lets the mint land behind it --
          // and submitting without one returns silently, so the button did nothing at all with
          // no spinner and no error to say why.
          disabled: busy || getFlow() === null || keyInput.trim() === "" || (withPicker && keyProvider === null),
          "data-e2e": "save-key",
          onclick: () => void send(() => submitKey(keyInput.trim(), keyProvider)),
        },
        "Save & finish",
      ),
    ]);

    const savedAs =
      selected !== null && selected.env_var !== ""
        ? m("p", { class: css.HINT }, `Saved as ${selected.env_var} for this mind.`)
        : null;

    if (!withPicker) {
      // A provider you have to subscribe to before a key exists gets that as an explicit first
      // step, rather than a sentence hoping you already did it.
      const signupUrl = method?.signup_url ?? "";
      if (signupUrl === "") {
        return m("div", [
          m("div", { class: css.SECTION_LABEL }, "Use an API key"),
          m("p", { class: css.LEAD }, method?.description ?? `Paste a ${current.provider_name} API key.`),
          stepLabel(null, "Your API key"),
          keyField,
          savedAs,
        ]);
      }
      // Same screen as any other pasted key -- one field and a button -- with the place to GET
      // the key named above it. It was a numbered 1-2 sequence, which read as a two-part
      // procedure when step one is "go to a website if you have not already". The link is the
      // instruction; the form is the whole task.
      return m("div", [
        m("div", { class: css.SECTION_LABEL }, "Use an API key"),
        m("p", { class: css.LEAD }, [
          method?.description ?? `Paste a ${current.provider_name} API key.`,
          " Get one at ",
          m(
            "a",
            { class: css.LEAD_LINK, href: signupUrl, target: "_blank", rel: "noopener noreferrer" },
            signupUrl.replace(/^https?:\/\//, ""),
          ),
          ".",
        ]),
        stepLabel(null, "Your API key"),
        keyField,
        savedAs,
      ]);
    }
    return m("div", [
      m("p", { class: css.LEAD }, method?.description ?? "Pick the provider, then paste its key."),
      stepBlock(1, false, [
        stepLabel("1", "Pick your provider"),
        m(
          "button",
          {
            type: "button",
            class: css.PICKER_TRIGGER,
            "aria-expanded": keyMenuOpen ? "true" : "false",
            onclick: (event: MouseEvent) => {
              keyMenuAnchor = (event.currentTarget as HTMLElement).getBoundingClientRect();
              keyMenuOpen = !keyMenuOpen;
            },
          },
          [
            selected !== null
              ? m("span", { class: css.PICKER_TRIGGER_VALUE }, [
                  m("span", { class: css.PICKER_TRIGGER_NAME }, selected.display),
                  m("span", { class: css.PICKER_TRIGGER_ENV }, selected.env_var),
                ])
              : m("span", { class: css.PICKER_TRIGGER_EMPTY }, "Choose a provider..."),
            m(
              "span",
              { class: `${css.PICKER_CARET} ${keyMenuOpen ? css.PICKER_CARET_OPEN : ""}` },
              m.trust(icon("chevron-down", { size: 15 })),
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
                    "data-e2e": `method-${candidate.id}`,
                    // Carry the account through. Picking another way in during a RE-AUTH is
                    // still that account's re-auth: dropping the id here would mint a new
                    // account and orphan every chat bound to the old one, on success.
                    onclick: () =>
                      begin(current, candidate, {
                        fromChooser: false,
                        accountId: reauthAccountId ?? undefined,
                      }),
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

  /** Start this account's own lane's primary sign-in, into the folder it already has. */
  async function reauthenticate(accountId: string, laneId: string): Promise<void> {
    const owner = getLanes().find((candidate) => candidate.id === laneId);
    if (owner === undefined) return;
    await begin(owner, owner.methods[0], { fromChooser: true, accountId });
  }

  return {
    async oninit() {
      await load();
      // Opened ON an account rather than to add one: a dead-account notice, or a provider
      // card whose credential expired. Land on that account's sign-in, not the lane list.
      const accountId = takeChooserAccountId();
      if (accountId === null) return;
      const account = getAccounts().find((candidate) => candidate.id === accountId);
      if (account !== undefined) await reauthenticate(account.id, account.lane);
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
      // `busy` excludes the PREVIOUS attempt's failure. `begin` clears `error` and sets
      // `busy`, but the flow object is only replaced once the POST returns -- and spawning a
      // PTY takes seconds -- so without this, "Try again" re-renders the identical error
      // screen it was clicked on and reads as a dead button. Each extra click starts another
      // sign-in, and each one kills the previous CLI.
      const isFailed = !busy && flow !== null && flow.status.state === "failed";
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
                disabled: busy,
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

      // Which of the four sizes this screen is. A verdict is a sentence and gets the small
      // panel; the lane list is the widest thing the flow shows. The body then sizes to its
      // content within that, so a two-line confirmation is a two-line panel rather than the
      // tallest screen's height filled out with white space.
      // What the panel is showing, which decides its size.
      //
      // A PENDING verdict deliberately keeps the size of the screen it interrupts. Checking a
      // credential can take well under a second, and resizing down to the small status panel and
      // straight back up reads as a flinch. A settled verdict -- signed in, or failed -- is a
      // screen the user will sit on, so that one takes the size it deserves.
      const settledScreen: "status" | "menu" | "form" | "chooser" =
        current === null ? "chooser" : mode === "menu" ? "menu" : "form";
      const screen: "status" | "menu" | "form" | "chooser" = isPending
        ? settledScreen
        : isSuccess || error !== null
          ? "status"
          : settledScreen;
      // The entry screen keeps a floor: the flow always returns there, and a panel that shrinks
      // under the pointer on the way back reads as something having gone wrong.
      const size = css.panelSize(screen);

      return m(
        "div.claude-login-overlay",
        // Dismissal keys off mouse DOWN, via the shared helper, because a click fires
        // wherever the press ENDED: selecting the device code or the several-hundred
        // character sign-in URL and releasing past the dialog's edge would otherwise read
        // as "close this" and abort the flow the code was being copied out of.
        backdropDismissAttrs(onClose),
        [
          current !== null && mode === "apiKey" ? keyProviderMenu(current) : null,
          m(
            "div",
            {
              class: css.MODAL,
              role: "dialog",
              "aria-modal": "true",
              "aria-label": "Pick your AI provider",
              "data-e2e": "provider-chooser",
            },
            m("div", { class: `${css.PANEL} ${size.width}` }, [
              m("div", { class: css.HEADER }, [
                current !== null
                  ? m(
                      "button",
                      { type: "button", class: css.BACK_BUTTON, onclick: back, "aria-label": "Back" },
                      m.trust(icon("chevron-left", { size: 16 })),
                    )
                  : null,
                m("h2", { class: css.TITLE }, title),
                // Which harness this connection will run on, stated top-right where the mockup
                // puts its harness picker. Not a picker here: provider -> harness is fixed in
                // V1, so there is nothing to choose -- but it is still the fact you want before
                // you hand over a credential, and the header is where the mockup says it goes.
                m("span", { class: css.HEADER_END }, [
                  current !== null && !isSuccess && !isPending
                    ? m("span", { class: css.RUNS_ON }, [
                        m("span", { class: css.RUNS_ON_PREFIX }, "Runs on"),
                        m("span", { class: css.RUNS_ON_NAME }, current.harness_label),
                      ])
                    : null,
                  m(
                    "button",
                    { type: "button", class: css.CLOSE_BUTTON, onclick: onClose, "aria-label": "Close" },
                    m.trust(icon("close", { size: 16 })),
                  ),
                ]),
              ]),
              m(
                "div",
                {
                  class: size.body,
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
                      m(
                        "button",
                        { type: "button", class: css.PRIMARY_BTN, "data-e2e": "done", onclick: onClose },
                        "Done",
                      ),
                    ),
                  )
                : null,
            ]),
          ),
        ],
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
