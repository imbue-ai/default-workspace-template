/**
 * The chat root's one notice: a failure of something done from the chat list (stop, restart, delete) or of a
 * message sent from outside a chat, told in a styled dialog over the list rather than a browser alert. Our sentence
 * on screen, the server's text behind "See details", Try again where repeating the operation can help, and "Report
 * a bug" when the sentence asks for one.
 */

import m from "mithril";
import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import { failureSentence } from "@imbue/workspace-ui/src/models/failure";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import { reportBugActions } from "../views/failure-actions";
import type { FailureLines } from "../views/failure-lines";

export interface RootNotice {
  readonly title: string;
  readonly lines: FailureLines;
  /** Repeats the operation; null when repeating it cannot help or is not safe. */
  readonly retry: (() => void) | null;
}

let current: RootNotice | null = null;

export function raiseRootNotice(notice: RootNotice): void {
  current = notice;
  m.redraw();
}

function closeRootNotice(): void {
  current = null;
  m.redraw();
}

export interface RootNoticeViewAttrs {
  /** The root's connection to the shell, null before it connects. */
  readonly connection: Pick<ShellConnection, "canReportBug" | "reportBug"> | null;
}

/** Mounted once by the chat root, over everything else. */
export function RootNoticeView(): m.Component<RootNoticeViewAttrs> {
  const dialog = makeNoticeDialog();
  return {
    view(vnode) {
      const notice = current;
      if (notice === null) return null;
      const retry = notice.retry;
      return m(dialog, {
        title: notice.title,
        body: notice.lines.body,
        details: notice.lines.details,
        dismissLabel: retry === null ? "OK" : "Cancel",
        onDismiss: closeRootNotice,
        actions: [
          ...reportBugActions(vnode.attrs.connection, notice.lines.isReportable),
          ...(retry === null
            ? []
            : [
                {
                  label: "Try again",
                  run: () => {
                    closeRootNotice();
                    retry();
                  },
                },
              ]),
        ],
      });
    },
  };
}

/** A Stop chat or Restart chat from the list, refused or unanswered. */
export function describeRunVerbFailure(error: unknown, title: string, isRestart: boolean): FailureLines {
  const failure = describeRequestFailure(error);
  const named = `"${title}"`;
  if (failure.cause === "not_answered") {
    return {
      body: [
        `The workspace didn't answer, so Imbue Studio can't tell whether ${named} ${isRestart ? "restarted" : "stopped"}.`,
      ],
      details: failure.details,
      isReportable: false,
    };
  }
  if (failure.kind === "chat_gone") {
    return { body: [failureSentence(failure).message], details: failure.details, isReportable: false };
  }
  const lead = isRestart
    ? `Couldn't restart ${named}. It's still stopped`
    : `Couldn't stop ${named}. It's still running`;
  if (failure.cause === "user_facing") {
    return {
      body: [`${lead}.`, failure.userFacingMessage],
      details: failure.details,
      isReportable: failure.isReportable,
    };
  }
  const told = failureSentence(failure, lead);
  return { body: [told.message], details: told.details, isReportable: told.isReportable };
}

/** A Delete chat from the list, refused or unanswered. */
export function describeDeleteFailure(error: unknown, title: string): FailureLines {
  const failure = describeRequestFailure(error);
  const named = `"${title}"`;
  if (failure.cause === "not_answered") {
    return {
      body: [`The workspace didn't answer, so Imbue Studio can't tell whether ${named} was deleted.`],
      details: failure.details,
      isReportable: false,
    };
  }
  const told = failureSentence(failure, `Couldn't delete ${named}. It's still here`);
  return { body: [told.message], details: told.details, isReportable: told.isReportable };
}

/** A message sent from outside a chat (the launcher, a desktop draft) that the chat would not take, or that got no
 *  answer; ``isDrafted`` when it was put back in the chat's composer. */
export function describeIntakeApplyFailure(error: unknown, isDrafted: boolean): FailureLines {
  const failure = describeRequestFailure(error);
  if (failure.cause === "not_answered") {
    return {
      body: ["The workspace didn't answer, so Imbue Studio can't tell whether your message reached this chat."],
      details: failure.details,
      isReportable: false,
    };
  }
  return {
    body: [
      isDrafted
        ? "Your message couldn't be sent to this chat. It's in the box below."
        : "Your message couldn't be sent to a chat.",
      failure.cause === "user_facing" ? failure.userFacingMessage : null,
    ],
    details: failure.details,
    isReportable: failure.cause === "user_facing" && failure.isReportable,
  };
}
