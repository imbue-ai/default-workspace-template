/**
 * The update notice, shown once as a top banner beside the staleness banner: the apply kept one
 * rollback point for everything it touched, and only a person closes it. The banner names what
 * the apply touched, since a rollback takes all of it back together. Three states, read off the
 * notice in the store's state, which the socket keeps current: open (Roll back / Everything
 * seems good), a rollback running (its progress, no verbs), and settled (the outcome, and
 * Close, beside an agent to finish the rollback when the update changed the workspace's own setup).
 * Roll back asks first, naming the update and the apps it restarts. A press that failed says which
 * one, with the shell's words behind "See details".
 */

import m from "mithril";
import { agentFixTask } from "@imbue/workspace-ui/src/app_contract";
import { ASKING_AN_AGENT_LABEL } from "@imbue/workspace-ui/src/components/FailureButtons";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { SeeDetails } from "@imbue/workspace-ui/src/components/SeeDetails";
import { bannerClass } from "@imbue/workspace-ui/src/components/banner";
import { DestroyConfirmDialog } from "@imbue/workspace-ui/src/DestroyConfirmDialog";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import { refusalKindOf } from "../model/failures";
import type { FailureNotice } from "../model/failures";
import {
  SHELL_APP_NAME,
  SYSTEM_SERVICES_RESTART_COMMAND,
  confirmUpdate,
  isNoticeSettled,
  isRollbackRunning,
  isWorkspaceOnlyNotice,
  rollbackUpdate,
} from "../model/UpdateNotice";
import type { UpdateNotice } from "../model/UpdateNotice";
import { appDisplayName } from "../reducers/desktopState";
import type { DesktopState } from "../reducers/desktopState";
import type { DesktopStore } from "../store/DesktopStore";
import { FailureLine } from "./FailureLine";

export const UPDATE_NOTICE_BANNER_MARKER = "update-notice-banner";

const OPEN_NOTICE_TEXT_SUFFIX = "If something is not working, you can go back to the previous version.";
export const SYSTEM_SERVICES_RESTART_DETAILS =
  "This update also changed the workspace's own setup, so after the files are restored an agent has to restart " +
  "the workspace to finish. This notice then offers to ask one.";
const ROLLBACK_FAILED = "Couldn't roll back";
const NOTICE_NOT_CLOSED = "Couldn't close this notice";
/** What a settled notice of such an update holds behind "See details": the command that finishes the rollback. */
export const SYSTEM_SERVICES_RESTART_SETTLED_DETAILS = `The workspace's system services restart with: ${SYSTEM_SERVICES_RESTART_COMMAND}`;
/** What an agent asked to finish a rollback is told: how the rollback ended, as the notice shows it, and the command
 *  that finishes one (a failed one may need looking at first). */
export function finishRollbackRequest(outcome: string): string {
  return agentFixTask({
    whatFailed: outcome,
    details: null,
    shown: "notice",
    instruction:
      "That is how the rollback of the last workspace update, which had changed how the workspace starts, ended. " +
      "Finish what it leaves to do; restarting the workspace's system services " +
      `(\`${SYSTEM_SERVICES_RESTART_COMMAND}\`) is what finishes such a rollback. Then tell the user what you did.`,
  });
}

/** The shell's display name is "Workspace", which reads as the whole workspace in a sentence. */
function displayName(state: DesktopState, appName: string): string {
  if (appName === SHELL_APP_NAME) return "the workspace interface";
  return appDisplayName(state, appName);
}

function listed(names: readonly string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** What the apply touched, as a sentence subject: the apps it named, or the workspace. */
function touchedText(state: DesktopState, notice: UpdateNotice): string {
  return isWorkspaceOnlyNotice(notice)
    ? "the workspace"
    : listed(notice.apps.map((appName) => displayName(state, appName)));
}

function capitalized(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function appliedAtText(notice: UpdateNotice): string {
  return new Date(notice.appliedAt * 1000).toLocaleString();
}

const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE;
const SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR;

/** How long before ``nowMs`` an update applied at ``appliedAtSeconds`` was, in words: the notice stays until someone
 *  confirms it, which can be days later. */
export function updatedAgoText(appliedAtSeconds: number, nowMs: number): string {
  const seconds = Math.max(0, nowMs / 1000 - appliedAtSeconds);
  if (seconds < SECONDS_PER_MINUTE) return "a moment ago";
  const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (seconds < SECONDS_PER_HOUR) return relative.format(-Math.floor(seconds / SECONDS_PER_MINUTE), "minute");
  if (seconds < SECONDS_PER_DAY) return relative.format(-Math.floor(seconds / SECONDS_PER_HOUR), "hour");
  return relative.format(-Math.floor(seconds / SECONDS_PER_DAY), "day");
}

/** What a rollback restarts, by the names people know: each program's apps, and the rest as other parts of the
 *  workspace. */
function restartedText(state: DesktopState, notice: UpdateNotice): string {
  const names: string[] = [];
  let isAnyUnnamed = false;
  for (const program of notice.programs) {
    const apps = state.apps.filter((app) => app.program === program).map((app) => displayName(state, app.name));
    if (program === SHELL_APP_NAME && apps.length === 0) apps.push(displayName(state, SHELL_APP_NAME));
    if (apps.length === 0) isAnyUnnamed = true;
    for (const name of apps) if (!names.includes(name)) names.push(name);
  }
  if (isAnyUnnamed) names.push("other parts of the workspace");
  if (names.length === 0) return "No app restarts on its own.";
  return `${capitalized(listed(names))} will restart.`;
}

/** The banner's state, as its progress and outcome define it (open, rolling back, settled). */
function stateKey(notice: UpdateNotice): string {
  return JSON.stringify([notice.progress, notice.outcome]);
}

export interface UpdateNoticeBannerAttrs {
  readonly store: DesktopStore;
}

/** "Ask an agent to finish it": it reads "Asking an agent…" and takes no press until the agent's chat is up. */
function finishButton(store: DesktopStore, request: string): m.Children {
  const isAsking = store.isAskingAgent(request);
  return m(
    Button,
    {
      sm: true,
      extra: "update-notice-finish",
      disabled: isAsking,
      onclick: () => void store.askAgentFromShell(request),
    },
    isAsking ? ASKING_AN_AGENT_LABEL : "Ask an agent to finish it",
  );
}

export function UpdateNoticeBanner(): m.Component<UpdateNoticeBannerAttrs> {
  let isDialogOpen = false;
  let isRequestInFlight = false;
  let error: FailureNotice | null = null;
  // The notice state the error was raised in: it explains why that state's verb did nothing, so it
  // goes once the notice moves on (a rollback's progress or outcome arriving from another window).
  let errorState: string | null = null;

  async function run(
    store: DesktopStore,
    verb: () => Promise<void>,
    notice: UpdateNotice,
    whatFailed: string,
  ): Promise<void> {
    isRequestInFlight = true;
    error = null;
    m.redraw();
    try {
      await verb();
    } catch (caught) {
      // A rollback the update-self script refused leaves the notice where it was, which only the workspace can
      // change, so it owes an agent though a refusal with a reason owes none by itself.
      const isStuck = refusalKindOf(describeRequestFailure(caught)) === "rollback_refused";
      error = store.failureNotice(whatFailed, caught, [], isStuck);
      errorState = stateKey(notice);
    } finally {
      isRequestInFlight = false;
      m.redraw();
    }
  }

  function verbs(store: DesktopStore, notice: UpdateNotice): m.Children {
    if (isRollbackRunning(notice)) return null;
    if (isNoticeSettled(notice)) {
      return [
        notice.needsSystemServicesRestart ? finishButton(store, finishRollbackRequest(notice.outcome ?? "")) : null,
        m(
          Button,
          {
            sm: true,
            extra: "update-notice-close",
            disabled: isRequestInFlight,
            onclick: () => void run(store, confirmUpdate, notice, NOTICE_NOT_CLOSED),
          },
          "Close",
        ),
      ];
    }
    return [
      m(
        Button,
        {
          sm: true,
          extra: "update-notice-rollback",
          disabled: isRequestInFlight,
          onclick: () => {
            isDialogOpen = true;
          },
        },
        "Roll back",
      ),
      m(
        Button,
        {
          sm: true,
          variant: "primary",
          extra: "update-notice-confirm",
          disabled: isRequestInFlight,
          onclick: () => void run(store, confirmUpdate, notice, NOTICE_NOT_CLOSED),
        },
        "Everything seems good",
      ),
    ];
  }

  function text(state: DesktopState, notice: UpdateNotice): string {
    if (isRollbackRunning(notice)) return `Rolling back: ${notice.progress}...`;
    if (isNoticeSettled(notice)) return notice.outcome ?? "";
    const verb = notice.apps.length > 1 ? "were" : "was";
    const ago = updatedAgoText(notice.appliedAt, Date.now());
    return `${capitalized(touchedText(state, notice))} ${verb} updated ${ago}. ${OPEN_NOTICE_TEXT_SUFFIX}`;
  }

  function dialog(store: DesktopStore, state: DesktopState, notice: UpdateNotice): m.Children {
    if (!isDialogOpen) return null;
    const apps = touchedText(state, notice);
    return m(DestroyConfirmDialog, {
      agentName: apps,
      title: "Roll back this update",
      question: [
        "Roll back the update applied ",
        m("strong", appliedAtText(notice)),
        " to ",
        m("strong", apps),
        `? Work saved since then is kept. ${restartedText(state, notice)}`,
      ],
      details: notice.needsSystemServicesRestart ? SYSTEM_SERVICES_RESTART_DETAILS : undefined,
      confirmLabel: "Roll back",
      onConfirm() {
        isDialogOpen = false;
        void run(store, rollbackUpdate, notice, ROLLBACK_FAILED);
      },
      onCancel() {
        isDialogOpen = false;
      },
    });
  }

  return {
    view(vnode) {
      const state = vnode.attrs.store.getState();
      const notice = state.updateNotice;
      if (notice === null) return null;
      if (error !== null && errorState !== stateKey(notice)) {
        error = null;
        errorState = null;
      }
      const marker = UPDATE_NOTICE_BANNER_MARKER;
      const tone = isNoticeSettled(notice) || isRollbackRunning(notice) ? "neutral" : "warning";
      return [
        m("div", { class: bannerClass(marker, tone) }, [
          m("div", { class: `${marker}-text min-w-0` }, [
            text(state, notice),
            isNoticeSettled(notice) && notice.needsSystemServicesRestart
              ? m(SeeDetails, { details: SYSTEM_SERVICES_RESTART_SETTLED_DETAILS })
              : null,
            error === null
              ? null
              : m(FailureLine, {
                  notice: error,
                  onReportBug: vnode.attrs.store.reportBugAction(),
                  marker: `${marker}-error`,
                  messageClass: "text-danger",
                }),
          ]),
          m("span", { class: "flex flex-none items-center gap-1.5" }, verbs(vnode.attrs.store, notice)),
        ]),
        dialog(vnode.attrs.store, state, notice),
      ];
    },
  };
}
