/**
 * A failure told on the surface it belongs to rather than in a toast (the launcher under its field, a dialog's line,
 * the backdrop, the update notice): its sentence, everything underneath it behind a collapsed "See details", and its
 * buttons, then "Report a bug" for a notice that offers one wherever a bug report can be opened as it draws.
 */

import m from "mithril";
import type { ReportedFailure } from "@imbue/workspace-ui/src/app_contract";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { SeeDetails } from "@imbue/workspace-ui/src/components/SeeDetails";
import { REPORT_A_BUG_LABEL } from "@imbue/workspace-ui/src/models/failure";
import type { ToastAction } from "@imbue/workspace-ui/src/models/toasts";
import { spinnerIcon } from "@imbue/workspace-ui/src/components/icons";
import type { FailureNotice } from "../model/failures";
import type { BootFailure } from "../store/DesktopStore";

/** Open the bug report about a failure; null where none can be opened now (the store's ``reportBugAction``). */
export type ReportBug = ((failure: ReportedFailure) => void) | null;

/** ``notice``'s buttons, then "Report a bug" about it where it offers one and ``onReportBug`` can open one. */
export function failureActions(notice: FailureNotice, onReportBug: ReportBug): readonly ToastAction[] {
  if (!notice.isBugReportOffered || onReportBug === null) return notice.actions;
  const report = { message: notice.message, details: notice.details };
  return [...notice.actions, { label: REPORT_A_BUG_LABEL, run: () => onReportBug(report), keepsToast: true }];
}

export interface FailureLineAttrs {
  readonly notice: FailureNotice;
  readonly onReportBug: ReportBug;
  /** The marker class the line is known by; its parts carry ``<marker>-message`` and ``<marker>-action``. */
  readonly marker: string;
  /** The sentence's own utilities: its colour and size on this surface. */
  readonly messageClass: string;
}

export const FailureLine: m.Component<FailureLineAttrs> = {
  view(vnode) {
    const { notice, marker, messageClass, onReportBug } = vnode.attrs;
    const actions = failureActions(notice, onReportBug);
    return m("div", { class: `${marker} flex min-w-0 flex-col items-start gap-1`, role: "alert" }, [
      m("p", { class: `${marker}-message m-0 ${messageClass}` }, notice.message),
      m(SeeDetails, { details: notice.details }),
      actions.length === 0
        ? null
        : m(
            "div",
            { class: "flex flex-wrap gap-1" },
            actions.map((action) => {
              const isPending = action.pending?.isPending() === true;
              return m(
                Button,
                { sm: true, extra: `${marker}-action`, disabled: isPending, onclick: action.run },
                isPending && action.pending !== undefined ? action.pending.label : action.label,
              );
            }),
          ),
    ]);
  },
};

/** The spinner the desktop shows while the startup read of the desktops and apps has not answered. */
export function bootSpinner(): m.Vnode {
  return m("span", { "data-boot-spinner": "", class: "boot-spinner text-faint", "aria-label": "Loading" }, [
    m.trust(spinnerIcon(28)),
  ]);
}

export interface BootFailureNoticeAttrs {
  readonly failure: BootFailure;
  readonly onReportBug: ReportBug;
}

/** Why nothing has answered the startup read of the desktops and apps, where the desktop would be, under the spinner
 *  of the read being tried again, until a read answers. */
export const BootFailureNotice: m.Component<BootFailureNoticeAttrs> = {
  view(vnode) {
    const { failure, onReportBug } = vnode.attrs;
    return m(
      "div",
      {
        "data-boot-failure": "",
        class:
          "boot-failure mx-4 flex max-w-md flex-col items-center gap-3 rounded-lg bg-surface px-5 py-4 text-left " +
          "shadow-raised",
      },
      [
        bootSpinner(),
        m(FailureLine, {
          notice: failure.notice,
          onReportBug,
          marker: "boot-failure-line",
          messageClass: "text-(length:--font-size-body) text-primary",
        }),
      ],
    );
  },
};
