/**
 * The update notice, shown once as a top banner beside the staleness banner: the apply kept one
 * rollback point for everything it touched, and only a person closes it. The banner names what
 * the apply touched, since a rollback takes all of it back together. Three states, all read off
 * the notice the socket keeps current: open (Roll back / Everything seems good), a rollback
 * running (its progress, no verbs), and settled (the outcome, and Close). Roll back asks first,
 * naming the update and what it restarts.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { bannerClass } from "@imbue/workspace-ui/src/components/banner";
import { DestroyConfirmDialog } from "@imbue/workspace-ui/src/DestroyConfirmDialog";
import { getApp } from "../models/Inventory";
import {
  SHELL_APP_NAME,
  confirmUpdate,
  getUpdateNotice,
  isNoticeSettled,
  isRollbackRunning,
  isWorkspaceOnlyNotice,
  rollbackUpdate,
} from "../models/UpdateNotice";
import type { UpdateNotice } from "../models/UpdateNotice";

export const UPDATE_NOTICE_BANNER_MARKER = "update-notice-banner";

const OPEN_NOTICE_TEXT_SUFFIX = "a moment ago. If something is not working, you can go back to the previous version.";
export const SYSTEM_SERVICES_RESTART_DETAILS =
  "This update also changed the workspace's own setup, so after the files are restored an agent has to " +
  "restart the workspace's system services before the previous version runs.";

/** The shell's display name is "Workspace", which reads as the whole workspace in a sentence. */
function displayName(appName: string): string {
  if (appName === SHELL_APP_NAME) return "the workspace interface";
  return getApp(appName)?.display_name ?? appName;
}

function listed(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** What the apply touched, as a sentence subject: the apps it named, or the workspace. */
function touchedText(notice: UpdateNotice): string {
  return isWorkspaceOnlyNotice(notice) ? "the workspace" : listed(notice.apps.map(displayName));
}

function capitalized(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function appliedAtText(notice: UpdateNotice): string {
  return new Date(notice.appliedAt * 1000).toLocaleString();
}

/** The banner's state, as its progress and outcome define it (open, rolling back, settled). */
function stateKey(notice: UpdateNotice): string {
  return JSON.stringify([notice.progress, notice.outcome]);
}

export function UpdateNoticeBanner(): m.Component {
  let isDialogOpen = false;
  let isRequestInFlight = false;
  let error: string | null = null;
  // The notice state the error was raised in: it explains why that state's verb did nothing, so it
  // goes once the notice moves on (a rollback's progress or outcome arriving from another window).
  let errorState: string | null = null;

  async function run(verb: () => Promise<void>, notice: UpdateNotice): Promise<void> {
    isRequestInFlight = true;
    error = null;
    m.redraw();
    try {
      await verb();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
      errorState = stateKey(notice);
    } finally {
      isRequestInFlight = false;
      m.redraw();
    }
  }

  function verbs(notice: UpdateNotice): m.Children {
    if (isRollbackRunning(notice)) return null;
    if (isNoticeSettled(notice)) {
      return m(
        Button,
        {
          sm: true,
          extra: "update-notice-close",
          disabled: isRequestInFlight,
          onclick: () => void run(confirmUpdate, notice),
        },
        "Close",
      );
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
          onclick: () => void run(confirmUpdate, notice),
        },
        "Everything seems good",
      ),
    ];
  }

  function text(notice: UpdateNotice): string {
    if (isRollbackRunning(notice)) return `Rolling back: ${notice.progress}...`;
    if (isNoticeSettled(notice)) return notice.outcome ?? "";
    const verb = notice.apps.length > 1 ? "were" : "was";
    return `${capitalized(touchedText(notice))} ${verb} updated ${OPEN_NOTICE_TEXT_SUFFIX}`;
  }

  function dialog(notice: UpdateNotice): m.Children {
    if (!isDialogOpen) return null;
    const apps = touchedText(notice);
    const restarts =
      notice.programs.length === 0 ? "no app restarts on its own" : `${listed(notice.programs)} will restart`;
    return m(DestroyConfirmDialog, {
      agentName: apps,
      title: "Roll back this update",
      question: [
        "Roll back the update applied ",
        m("strong", appliedAtText(notice)),
        " to ",
        m("strong", apps),
        `? Work saved since then is kept; ${restarts}.`,
      ],
      details: notice.needsSystemServicesRestart ? SYSTEM_SERVICES_RESTART_DETAILS : undefined,
      confirmLabel: "Roll back",
      onConfirm() {
        isDialogOpen = false;
        void run(rollbackUpdate, notice);
      },
      onCancel() {
        isDialogOpen = false;
      },
    });
  }

  return {
    view() {
      const notice = getUpdateNotice();
      if (notice === null) return null;
      if (error !== null && errorState !== stateKey(notice)) {
        error = null;
        errorState = null;
      }
      const marker = UPDATE_NOTICE_BANNER_MARKER;
      const tone = isNoticeSettled(notice) || isRollbackRunning(notice) ? "neutral" : "warning";
      return [
        m("div", { class: bannerClass(marker, tone) }, [
          m("span", { class: `${marker}-text min-w-0` }, [
            text(notice),
            error === null ? null : m("span", { class: `${marker}-error ml-2 text-danger` }, error),
          ]),
          m("span", { class: "flex flex-none items-center gap-1.5" }, verbs(notice)),
        ]),
        dialog(notice),
      ];
    },
  };
}
