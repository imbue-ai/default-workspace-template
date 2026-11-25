/**
 * How the desktop shows a failure on its own surfaces (a toast, a dialog's line, the launcher, the backdrop, a
 * window's placeholder): a sentence, what goes behind "See details", and its buttons. A failed request's sentence is
 * ``failureSentence``'s; the store adds the buttons, an agent's among them. "Report a bug" is not among them: the
 * surface offers it as it draws, for a notice that says so, wherever the embedder opens a bug report by then (the
 * store's ``reportBugAction``).
 */

import type { Failure } from "@imbue/workspace-ui/src/models/failure";
import type { LaunchPath } from "./records";
import type { ToastAction } from "@imbue/workspace-ui/src/models/toasts";

/** Why the only desktop cannot be deleted, on its disabled Delete. */
export const ONLY_DESKTOP_REASON = "You can't delete your only desktop. Add another one first.";

/** A failure as a desktop surface shows it. */
export interface FailureNotice {
  readonly message: string;
  /** What goes behind "See details"; null when there is nothing beyond the sentence. */
  readonly details: string | null;
  /** Its buttons, in order. */
  readonly actions: readonly ToastAction[];
  /** The failure owes the person more help than its sentence gives: a toast of it stays. */
  readonly isHelpOwed: boolean;
  /** No agent's button is among ``actions`` for a failure that owes help (the ask would go through what failed, no
   *  app takes one, or this is a refused ask's own failure): the surface offers "Report a bug" after the buttons
   *  where it can. */
  readonly isBugReportOffered: boolean;
}

const REFUSAL_KIND_VALUES = [
  "app_unavailable",
  "launch_path_gone",
  "window_gone",
  "out_of_space",
  "not_saved",
  "launched_not_opened_out_of_space",
  "launched_not_opened",
  "desktop_gone",
  "rollback_refused",
] as const;

/** What the shell's refusals name with ``kind`` (desktop contracts.md, the error body), for the desktop to word
 *  with what only it holds. */
export type RefusalKind = (typeof REFUSAL_KIND_VALUES)[number];

const REFUSAL_KINDS: ReadonlySet<string> = new Set<RefusalKind>(REFUSAL_KIND_VALUES);

/** What an agent asked to free up space is told to do about a failure a full disk caused. */
export const FREE_SPACE_INSTRUCTION =
  "Free up disk space in this workspace, then tell the user what was taking up the space and what you removed.";

/** How a note about a launch the app took names it: a message sent ("Your message was sent to Chat"), text drafted
 *  into the app, or else the launch path by its label ("“New doc” went through in Docs"). The shell's
 *  ``_launch_lead`` says the same of a launch whose window it could not open. */
export function tookLaunchLead(displayName: string, launchPath: LaunchPath | null, isAsk = false): string {
  if (launchPath === null) return `${displayName} took that`;
  if (launchPath.text_param !== null) {
    // An ask's message is the shell's, not something the person wrote.
    return isAsk ? `The request was sent to ${displayName}` : `Your message was sent to ${displayName}`;
  }
  if (launchPath.draft_param !== null) return `Your text was put into ${displayName}`;
  return `“${launchPath.label}” went through in ${displayName}`;
}

/** What a window, a shortcut, or a refusal says of an app the workspace has no record of: its app list has none by
 *  that name, which is so of an app taken out and of one whose record could not be read alike. ``outcome`` is what
 *  the request then did not do ("nothing was opened"), or null for a window, which did nothing. */
export function appNotFoundSentence(displayName: string | null, outcome: string | null): string {
  const subject = displayName ?? "This app";
  return outcome === null
    ? `${subject} couldn't be found in this workspace.`
    : `${subject} couldn't be found in this workspace, so ${outcome}.`;
}

/** The kind of refusal the shell answered ``failure`` with, or null for any other failure. */
export function refusalKindOf(failure: Failure): RefusalKind | null {
  return failure.kind !== null && REFUSAL_KINDS.has(failure.kind) ? (failure.kind as RefusalKind) : null;
}

/** Whether pressing the same control again is what ``failure``'s words ask for: the fallback sentence ("Try
 *  again.", for a failure with no reason or a reason that is not a refusal) or a read nothing answered. Never a
 *  change nothing answered, which may have gone through, nor a refusal with its reason. */
export function isRetryable(failure: Failure): boolean {
  switch (failure.cause) {
    case "no_reason":
      return true;
    case "reason":
      return failure.status === null || failure.status < 400 || failure.status >= 500;
    case "not_answered":
      return failure.requestKind === "read";
    case "user_facing":
      return false;
  }
}
