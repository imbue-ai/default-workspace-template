/**
 * The band a recently updated critical app's tabs carry (and, as the banner, the shell's own
 * page): the apply kept a rollback point, and only a person closes it. Three states, all read
 * off the notice the socket keeps current: open (Roll back / Everything seems good), a rollback
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
  isNoticeSettled,
  isRollbackRunning,
  rollbackUpdate,
  updateNoticeForApp,
} from "../models/UpdateNotice";
import type { UpdateNotice } from "../models/UpdateNotice";

export interface UpdateNoticeBandAttrs {
  /** The app whose tabs carry the band; the shell's own name for the banner. */
  appName: string;
  /** The bare class tests and styles find the band by. */
  marker?: string;
}

export const UPDATE_NOTICE_BAND_MARKER = "update-notice-band";

export const OPEN_NOTICE_TEXT =
  "This app was updated a moment ago. If something is not working, you can go back to the previous version.";
export const OPEN_SHELL_NOTICE_TEXT =
  "The workspace interface was updated a moment ago. If something is not working, you can go back to the previous version.";
export const SERVICES_RESTART_DETAILS =
  "This update also changed the workspace's own setup, so after the files are restored an agent has to " +
  "restart the workspace before the previous version runs.";

function displayName(appName: string): string {
  return getApp(appName)?.display_name ?? appName;
}

function listed(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

function appliedAtText(notice: UpdateNotice): string {
  return new Date(notice.appliedAt * 1000).toLocaleString();
}

export function UpdateNoticeBand(): m.Component<UpdateNoticeBandAttrs> {
  let isDialogOpen = false;
  let isRequestInFlight = false;
  let error: string | null = null;

  async function run(verb: () => Promise<void>): Promise<void> {
    isRequestInFlight = true;
    error = null;
    m.redraw();
    try {
      await verb();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
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
          onclick: () => void run(confirmUpdate),
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
          onclick: () => void run(confirmUpdate),
        },
        "Everything seems good",
      ),
    ];
  }

  function text(notice: UpdateNotice, appName: string): string {
    if (isRollbackRunning(notice)) return `Rolling back: ${notice.progress}...`;
    if (isNoticeSettled(notice)) return notice.outcome ?? "";
    return appName === SHELL_APP_NAME ? OPEN_SHELL_NOTICE_TEXT : OPEN_NOTICE_TEXT;
  }

  function dialog(notice: UpdateNotice): m.Children {
    if (!isDialogOpen) return null;
    const apps = listed(notice.apps.map(displayName));
    return m(DestroyConfirmDialog, {
      agentName: apps,
      title: "Roll back this update",
      question: [
        "Roll back the update applied ",
        m("strong", appliedAtText(notice)),
        " to ",
        m("strong", apps),
        `? Work saved since then is kept; ${listed(notice.programs)} will restart.`,
      ],
      details: notice.needsServicesRestart ? SERVICES_RESTART_DETAILS : undefined,
      confirmLabel: "Roll back",
      onConfirm() {
        isDialogOpen = false;
        void run(rollbackUpdate);
      },
      onCancel() {
        isDialogOpen = false;
      },
    });
  }

  return {
    view(vnode) {
      const { appName } = vnode.attrs;
      const marker = vnode.attrs.marker ?? UPDATE_NOTICE_BAND_MARKER;
      const notice = updateNoticeForApp(appName);
      if (notice === null) return null;
      const tone = isNoticeSettled(notice) || isRollbackRunning(notice) ? "neutral" : "warning";
      return [
        m("div", { class: bannerClass(marker, tone) }, [
          m("span", { class: `${marker}-text min-w-0` }, [
            text(notice, appName),
            error === null ? null : m("span", { class: `${marker}-error ml-2 text-danger` }, error),
          ]),
          m("span", { class: "flex flex-none items-center gap-1.5" }, verbs(notice)),
        ]),
        dialog(notice),
      ];
    },
  };
}
