/**
 * A failure told on the surface it belongs to rather than in a toast (the launcher under its field, a dialog's line,
 * the backdrop, the update notice): its sentence, everything underneath it behind a collapsed "See details", and its
 * buttons.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { SeeDetails } from "@imbue/workspace-ui/src/components/SeeDetails";
import type { FailureNotice } from "../model/failures";

export interface FailureLineAttrs {
  readonly notice: FailureNotice;
  /** The marker class the line is known by; its parts carry ``<marker>-message`` and ``<marker>-action``. */
  readonly marker: string;
  /** The sentence's own utilities: its colour and size on this surface. */
  readonly messageClass: string;
}

export const FailureLine: m.Component<FailureLineAttrs> = {
  view(vnode) {
    const { notice, marker, messageClass } = vnode.attrs;
    return m("div", { class: `${marker} flex min-w-0 flex-col items-start gap-1`, role: "alert" }, [
      m("p", { class: `${marker}-message m-0 ${messageClass}` }, notice.message),
      m(SeeDetails, { details: notice.details }),
      notice.actions.length === 0
        ? null
        : m(
            "div",
            { class: "flex flex-wrap gap-1" },
            notice.actions.map((action) =>
              m(Button, { sm: true, extra: `${marker}-action`, onclick: action.run }, action.label),
            ),
          ),
    ]);
  },
};

export interface BootFailureNoticeAttrs {
  readonly notice: FailureNotice;
}

/** Why the startup read of the desktops and apps failed, where the desktop would be, until a read succeeds. */
export const BootFailureNotice: m.Component<BootFailureNoticeAttrs> = {
  view(vnode) {
    return m(
      "div",
      {
        "data-boot-failure": "",
        class: "boot-failure mx-4 max-w-md rounded-lg bg-surface px-5 py-4 text-left shadow-raised",
      },
      m(FailureLine, {
        notice: vnode.attrs.notice,
        marker: "boot-failure-line",
        messageClass: "text-(length:--font-size-body) text-primary",
      }),
    );
  },
};
