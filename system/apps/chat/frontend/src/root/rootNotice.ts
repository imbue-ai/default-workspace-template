/**
 * The chat root's one notice: a failure of something done from the chat list (stop, restart, delete) or of a
 * message sent from outside a chat, told in a styled dialog over the list rather than a browser alert. Our sentence
 * on screen, the server's text behind "See details", Try again where repeating the operation can help, and, for a
 * failure that owes help, "Ask an agent to fix this", or "Report a bug" where no agent can be asked.
 */

import m from "mithril";
import { askAgentToFix } from "@imbue/workspace-ui/src/app_contract";
import type { AgentFixRequest, ShellConnection, ShownFailure } from "@imbue/workspace-ui/src/app_contract";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import { failureSentence } from "@imbue/workspace-ui/src/models/failure";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import { failureRemedyActions } from "../views/failure-actions";
import { plainFailureSentence, withRefusedAsk } from "../views/failure-lines";
import type { FailureLines } from "../views/failure-lines";

export interface RootNotice {
  readonly title: string;
  readonly lines: FailureLines;
  /** Repeats the operation; null when repeating it cannot help or is not safe. */
  readonly retry: (() => void) | null;
  /** Offer no agent: the ask would go through what failed (an ask reaches its chat through the same intake route a
   *  message from outside a chat does). */
  readonly isAgentWithheld?: boolean;
}

let current: RootNotice | null = null;
// The notice's "Ask an agent to fix this" while it is under way, and why it was refused once it was.
let isAsking = false;
let askFailure: ShownFailure | null = null;

export function raiseRootNotice(notice: RootNotice): void {
  current = notice;
  isAsking = false;
  askFailure = null;
  m.redraw();
}

function closeRootNotice(): void {
  current = null;
  isAsking = false;
  askFailure = null;
  m.redraw();
}

function fixRequestOf(notice: RootNotice): AgentFixRequest {
  return { whatFailed: plainFailureSentence(notice.lines), details: notice.lines.details };
}

/** Hand ``notice``'s failure to an agent through the shell, which shows its chat; that closes the notice. A refused
 *  ask is told in the notice, under the failure it was about. */
async function askAgentAbout(notice: RootNotice, connection: Pick<ShellConnection, "askAgent">): Promise<void> {
  if (isAsking) return;
  isAsking = true;
  askFailure = null;
  m.redraw();
  const outcome = await askAgentToFix(connection, fixRequestOf(notice), { showsOutcome: true });
  // Another notice took this one's place meanwhile.
  if (current !== notice) return;
  isAsking = false;
  if (outcome.isOpened) {
    closeRootNotice();
    return;
  }
  askFailure = outcome.failure;
  m.redraw();
}

/** Leave the request a refused ask would have sent in a chat's message box, unsent; a draft that was made closes the
 *  notice, and the shell tells one that could not be. */
async function draftAskAbout(notice: RootNotice, connection: Pick<ShellConnection, "askAgent">): Promise<void> {
  const outcome = await askAgentToFix(connection, fixRequestOf(notice), { showsOutcome: true }, "draft");
  if (current === notice && outcome.isOpened) closeRootNotice();
}

export interface RootNoticeViewAttrs {
  /** The root's connection to the shell, null before it connects. */
  readonly connection: Pick<ShellConnection, "hasShell" | "askAgent" | "canReportBug" | "reportBug"> | null;
}

/** Mounted once by the chat root, over everything else. */
export function RootNoticeView(): m.Component<RootNoticeViewAttrs> {
  const dialog = makeNoticeDialog();
  return {
    view(vnode) {
      const notice = current;
      if (notice === null) return null;
      const { connection } = vnode.attrs;
      const retry = notice.retry;
      const told = withRefusedAsk(notice.lines, askFailure);
      const ask =
        !notice.lines.isHelpOwed || notice.isAgentWithheld === true || connection === null || !connection.hasShell()
          ? null
          : {
              isAsking,
              run: () => void askAgentAbout(notice, connection),
              refusal: askFailure,
              draft: () => void draftAskAbout(notice, connection),
            };
      return m(dialog, {
        title: notice.title,
        body: told.body,
        details: told.details,
        dismissLabel: retry === null && ask === null ? "OK" : "Cancel",
        isDismissable: !isAsking,
        onDismiss: closeRootNotice,
        actions: [
          ...failureRemedyActions(connection, told, ask),
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
        ].map((action) => (isAsking ? { ...action, isDisabled: true } : action)),
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
      isHelpOwed: false,
    };
  }
  if (failure.kind === "chat_gone") {
    const gone = failureSentence(failure);
    return { body: [gone.message], details: gone.details, isHelpOwed: gone.isHelpOwed };
  }
  const lead = isRestart
    ? `Couldn't restart ${named}. It's still stopped`
    : `Couldn't stop ${named}. It's still running`;
  if (failure.cause === "user_facing") {
    return {
      body: [`${lead}.`, failure.userFacingMessage],
      details: failure.details,
      isHelpOwed: !failure.isExpected,
    };
  }
  const told = failureSentence(failure, lead);
  return { body: [told.message], details: told.details, isHelpOwed: told.isHelpOwed };
}

/** A Delete chat from the list, refused or unanswered. */
export function describeDeleteFailure(error: unknown, title: string): FailureLines {
  const failure = describeRequestFailure(error);
  const named = `"${title}"`;
  if (failure.cause === "not_answered") {
    return {
      body: [`The workspace didn't answer, so Imbue Studio can't tell whether ${named} was deleted.`],
      details: failure.details,
      isHelpOwed: false,
    };
  }
  const told = failureSentence(failure, `Couldn't delete ${named}. It's still here`);
  return { body: [told.message], details: told.details, isHelpOwed: told.isHelpOwed };
}

/** A message sent from outside a chat (the launcher, a desktop draft) that the chat would not take, or that got no
 *  answer; ``isDrafted`` when it was put back in the chat's composer. */
export function describeIntakeApplyFailure(error: unknown, isDrafted: boolean): FailureLines {
  const failure = describeRequestFailure(error);
  if (failure.cause === "not_answered") {
    return {
      body: ["The workspace didn't answer, so Imbue Studio can't tell whether your message reached this chat."],
      details: failure.details,
      isHelpOwed: false,
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
    isHelpOwed: failureSentence(failure).isHelpOwed,
  };
}
