/**
 * The first run's provider page: "Your own cloud computer, any AI".
 *
 * Imbue Studio's first run ends here, inside the new workspace: the shell covers the desktop with this
 * page until its owner connects an AI provider or skips. The tiles are the lanes; a tile opens that
 * lane's sign-in in the provider chooser, which does the signing in. Once an account is connected it is
 * listed, more can be added, Skip goes away and Next leaves. With two or more connected the list marks the
 * default, the account the first conversation and every new chat run on, and a click on another row makes that
 * one the default. Skip and Next both finish the step for good (`finishFirstRunProviderStep`), after which the
 * shell takes its cover down.
 */

import m from "mithril";

import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import type { Lane, ProviderAccount } from "../models/Providers";
import {
  areLanesLoaded,
  closeProviderChooser,
  finishFirstRunProviderStep,
  getAccounts,
  getLanes,
  getSelectedAccount,
  isFirstRunProviderStepPending,
  isProviderChooserOpen,
  loadAccounts,
  loadLanesAndAccountsWithRetry,
  openProviderChooser,
  setDefaultAccount,
} from "../models/Providers";
import { reportProviderChooserOnScreenOnce } from "../shell";
import { ProviderChooserModal } from "./ProviderChooserModal";
import { providerMark } from "./providerMarks";

export const FIRST_RUN_PROVIDERS_TITLE = "Your own cloud computer, any AI";
export const FIRST_RUN_PROVIDERS_LINE = "Studio runs your agents and apps 24/7. Connect your provider to start.";
export const FIRST_RUN_CONNECTED_HEADING = "Connected";
export const FIRST_RUN_DEFAULT_TAG = "Default";
export const FIRST_RUN_MAKE_DEFAULT_HINT = "Make default";
export const FIRST_RUN_ADD_ANOTHER_HEADING = "Add another (optional)";
export const FIRST_RUN_ADD_ANOTHER_LINE =
  "Studio can use multiple subscriptions and switch between providers mid-conversation.";
export const FIRST_RUN_SKIP_LABEL = "Skip";
export const FIRST_RUN_NEXT_LABEL = "Next";

/** What each tile says under the provider's name: shorter than the chooser's own line, which has a row to itself. */
const TILE_LINE_BY_LANE: Readonly<Record<string, string>> = {
  anthropic: "Pro, Max or Ultra",
  openai: "ChatGPT plan",
  google: "Google account",
  "opencode-go": "Open models",
  openrouter: "Many providers",
  "api-key": "Any provider",
};

const MARK_SIZE = 26;

const ACCOUNT_ROW_CLASS = "flex items-center gap-3 px-4 py-2";
const ACCOUNT_TAG_CLASS = "first-run-muted ml-auto shrink-0 pl-3 text-[13px] leading-snug";

/** Pin ``accountId`` as the default. A failure is reloaded over, so the tag never sits on an account the chat app
 *  does not hold as the default. */
function makeDefault(accountId: string): void {
  void setDefaultAccount(accountId, true)
    .catch((error: unknown) => {
      console.warn(`Could not make account ${accountId} the default`, error);
      void loadAccounts();
    })
    .finally(() => {
      m.redraw();
    });
}

export interface FirstRunProvidersPageAttrs {
  shell: ShellConnection;
}

/** Tell the chrome the provider ask is on screen, once its tiles are drawn. */
function reportOnScreen(): void {
  if (areLanesLoaded()) reportProviderChooserOnScreenOnce(true);
}

export function FirstRunProvidersPage(): m.Component<FirstRunProvidersPageAttrs> {
  let isLeaving = false;
  let leaveError = "";

  function leave(shell: ShellConnection): void {
    if (isLeaving) return;
    isLeaving = true;
    leaveError = "";
    finishFirstRunProviderStep().then(
      () => {
        shell.firstRunProviderStep(false);
        m.redraw();
      },
      () => {
        isLeaving = false;
        leaveError = "That didn't go through. Try again.";
        m.redraw();
      },
    );
  }

  function laneTile(lane: Lane): m.Children {
    return m(
      "button",
      {
        key: lane.id,
        type: "button",
        "data-first-run-lane": lane.id,
        class: "first-run-tile",
        onclick: () => openProviderChooser({ laneId: lane.id }),
      },
      [
        m("span", { class: "first-run-tile-mark", "aria-hidden": "true" }, m.trust(providerMark(lane.id, MARK_SIZE))),
        m("span", { class: "flex min-w-0 flex-col text-left" }, [
          m("span", { class: "text-[16px] font-semibold leading-tight" }, lane.provider_name),
          m(
            "span",
            { class: "first-run-muted text-[13px] leading-snug" },
            TILE_LINE_BY_LANE[lane.id] ?? lane.subtitle,
          ),
        ]),
      ],
    );
  }

  /** One connected account. ``defaultAccountId`` is null while there is one account, which has no default to tell
   *  apart and none to choose: the row is then the account alone. */
  function accountRow(account: ProviderAccount, defaultAccountId: string | null): m.Children {
    const identity = [
      m(
        "span",
        { class: "first-run-tile-mark", "aria-hidden": "true" },
        m.trust(providerMark(account.lane, MARK_SIZE)),
      ),
      m("span", { class: "flex min-w-0 flex-col" }, [
        m("span", { class: "text-[16px] font-semibold leading-tight" }, account.provider),
        m("span", { class: "first-run-muted text-[13px] leading-snug" }, account.harness_label),
      ]),
    ];
    if (defaultAccountId === null) {
      return m("div", { key: account.id, "data-first-run-account": account.id, class: ACCOUNT_ROW_CLASS }, identity);
    }
    if (account.id === defaultAccountId) {
      return m("div", { key: account.id, "data-first-run-account": account.id, class: ACCOUNT_ROW_CLASS }, [
        ...identity,
        m("span", { "data-first-run-default": "", class: ACCOUNT_TAG_CLASS }, FIRST_RUN_DEFAULT_TAG),
      ]);
    }
    return m(
      "button",
      {
        key: account.id,
        type: "button",
        "data-first-run-account": account.id,
        class: `first-run-account-choice w-full text-left ${ACCOUNT_ROW_CLASS}`,
        onclick: () => makeDefault(account.id),
      },
      [...identity, m("span", { class: `first-run-account-hint ${ACCOUNT_TAG_CLASS}` }, FIRST_RUN_MAKE_DEFAULT_HINT)],
    );
  }

  return {
    oninit() {
      void loadLanesAndAccountsWithRetry().then(() => m.redraw());
    },
    // The chrome keeps its cover over a new workspace until a provider ask is on screen: this page is that ask.
    oncreate: reportOnScreen,
    onupdate: reportOnScreen,
    view(vnode) {
      const { shell } = vnode.attrs;
      // Finished here or in another window: nothing is drawn while the shell takes its cover down.
      if (!isFirstRunProviderStepPending() && isLeaving) return m("div", { class: "first-run-page" });
      const accounts = getAccounts();
      const hasAccount = accounts.length > 0;
      const defaultAccountId = accounts.length > 1 ? (getSelectedAccount()?.id ?? null) : null;
      return m("div", { id: "first-run-providers", class: "first-run-page" }, [
        m("div", { class: "flex w-full max-w-[660px] flex-col" }, [
          m("h1", { class: "first-run-headline text-center" }, FIRST_RUN_PROVIDERS_TITLE),
          m("p", { class: "first-run-lead mt-3 text-center" }, FIRST_RUN_PROVIDERS_LINE),
          hasAccount
            ? m("div", { class: "mt-10 flex flex-col" }, [
                m("h2", { class: "px-4 text-[15px] font-semibold" }, FIRST_RUN_CONNECTED_HEADING),
                m(
                  "div",
                  { class: "mt-2 flex flex-col" },
                  accounts.map((account) => accountRow(account, defaultAccountId)),
                ),
                m("h2", { class: "mt-6 px-4 text-[15px] font-semibold" }, FIRST_RUN_ADD_ANOTHER_HEADING),
                m("p", { class: "first-run-muted mt-1 px-4 text-[15px] leading-relaxed" }, FIRST_RUN_ADD_ANOTHER_LINE),
              ])
            : null,
          m(
            "div",
            { class: `${hasAccount ? "mt-4" : "mt-12"} grid grid-cols-1 gap-2.5 sm:grid-cols-3` },
            getLanes().map(laneTile),
          ),
          leaveError === "" ? null : m("p", { class: "mt-6 text-center text-[14px]", role: "alert" }, leaveError),
          // Skip is for leaving with nothing connected; once something is, Next is the one way on.
          m("div", { class: `mt-12 flex items-center ${hasAccount ? "justify-end" : "justify-between"}` }, [
            hasAccount
              ? null
              : m(
                  "button",
                  {
                    id: "first-run-skip",
                    type: "button",
                    class: "first-run-skip",
                    disabled: isLeaving,
                    onclick: () => leave(shell),
                  },
                  FIRST_RUN_SKIP_LABEL,
                ),
            m(
              "button",
              {
                id: "first-run-next",
                type: "button",
                class: "first-run-next",
                disabled: !hasAccount || isLeaving,
                onclick: () => leave(shell),
              },
              FIRST_RUN_NEXT_LABEL,
            ),
          ]),
        ]),
        isProviderChooserOpen() ? m(ProviderChooserModal, { onDismiss: closeProviderChooser }) : null,
      ]);
    },
  };
}
