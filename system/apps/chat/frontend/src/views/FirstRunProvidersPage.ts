/**
 * The first run's provider page: "Your own cloud computer, any AI".
 *
 * Imbue Studio's first run ends here, inside the new workspace: the shell covers the desktop with this
 * page until its owner connects an AI provider or skips. The tiles are the lanes; a tile opens that
 * lane's sign-in in the provider chooser, which does the signing in. Once an account is connected it is
 * listed, more can be added, Skip goes away and Next leaves. Skip and Next both finish the step for good
 * (`finishFirstRunProviderStep`), after which the shell takes its cover down.
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
  isFirstRunProviderStepPending,
  isProviderChooserOpen,
  loadLanesAndAccountsWithRetry,
  openProviderChooser,
} from "../models/Providers";
import { reportProviderChooserOnScreenOnce } from "../shell";
import { ProviderChooserModal } from "./ProviderChooserModal";
import { providerMark } from "./providerMarks";

export const FIRST_RUN_PROVIDERS_TITLE = "Your own cloud computer, any AI";
export const FIRST_RUN_PROVIDERS_LINE = "Studio runs your agents 24/7. Connect your provider to start.";
export const FIRST_RUN_CONNECTED_HEADING = "Connected";
export const FIRST_RUN_ADD_ANOTHER_HEADING = "Add another";
export const FIRST_RUN_ADD_ANOTHER_LINE =
  "Add more providers, or several plans from the same one. Studio lets you switch between them mid-conversation.";
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

  function accountRow(account: ProviderAccount): m.Children {
    return m(
      "div",
      { key: account.id, "data-first-run-account": account.id, class: "flex items-center gap-3 px-4 py-2" },
      [
        m(
          "span",
          { class: "first-run-tile-mark", "aria-hidden": "true" },
          m.trust(providerMark(account.lane, MARK_SIZE)),
        ),
        m("span", { class: "flex min-w-0 flex-col" }, [
          m("span", { class: "text-[16px] font-semibold leading-tight" }, account.provider),
          m("span", { class: "first-run-muted text-[13px] leading-snug" }, account.harness_label),
        ]),
      ],
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
      return m("div", { id: "first-run-providers", class: "first-run-page" }, [
        m("div", { class: "flex w-full max-w-[660px] flex-col" }, [
          m("h1", { class: "first-run-headline text-center" }, FIRST_RUN_PROVIDERS_TITLE),
          m("p", { class: "mt-3 text-center text-[18px] font-medium" }, FIRST_RUN_PROVIDERS_LINE),
          hasAccount
            ? m("div", { class: "mt-10 flex flex-col" }, [
                m("h2", { class: "px-4 text-[15px] font-semibold" }, FIRST_RUN_CONNECTED_HEADING),
                m("div", { class: "mt-2 flex flex-col" }, accounts.map(accountRow)),
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
