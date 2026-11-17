/**
 * How the desktop shows a failure on its own surfaces (a toast, a dialog's line, the launcher, the backdrop, a
 * window's placeholder): a sentence, what goes behind "See details", and its buttons. A failed request's sentence is
 * ``failureSentence``'s; the store adds the buttons. "Report a bug" is not among them: the surface offers it as it
 * draws, wherever the embedder opens a bug report by then (the store's ``reportBugAction``).
 */

import type { Failure } from "@imbue/workspace-ui/src/models/failure";
import type { AppRecord } from "./records";
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
  /** The sentence asks for a bug report: the surface offers "Report a bug" after the buttons where it can, and a
   *  toast of it stays even where it cannot. */
  readonly isReportable: boolean;
  /** An app whose not running is all the notice says: a toast of it goes once the app list shows the app running. */
  readonly untilRunning?: string;
}

const REFUSAL_KIND_VALUES = [
  "app_unavailable",
  "launch_path_gone",
  "window_gone",
  "out_of_space",
  "not_saved",
  "app_stopped",
  "app_could_not_start",
  "app_start_budget_spent",
  "app_still_starting",
  "launched_not_opened_out_of_space",
  "launched_not_opened",
] as const;

/** What the shell's refusals name with ``kind`` (desktop contracts.md, the error body), for the desktop to word
 *  with what only it holds. */
export type RefusalKind = (typeof REFUSAL_KIND_VALUES)[number];

const REFUSAL_KINDS: ReadonlySet<string> = new Set<RefusalKind>(REFUSAL_KIND_VALUES);

/** What a window, a shortcut, or a launcher row says of an app that is not running and that the workspace does not
 *  start on a request: one it does not run itself, or one it treats as part of how it runs. */
export function stoppedSentence(app: AppRecord): string {
  return app.program === "" ? `${app.display_name} isn't running.` : `${app.display_name} is not running right now.`;
}

/** What an agent asked to look at an app that could not start is told (the could-not-start page asks the same). */
export function couldNotStartRequest(app: AppRecord): string {
  return (
    `${app.display_name} couldn't start in this workspace. Its program's log may say why ` +
    `(supervisorctl tail ${app.program} stderr). Please find out why and get it running again.`
  );
}

/** The kind of refusal the shell answered ``failure`` with, or null for any other failure. */
export function refusalKindOf(failure: Failure): RefusalKind | null {
  return failure.kind !== null && REFUSAL_KINDS.has(failure.kind) ? (failure.kind as RefusalKind) : null;
}

/** Whether pressing the same control again is what ``failure``'s words ask for: the fallback sentence ("Try again.
 *  If this persists, report a bug.", for a failure with no reason or a reason that is not a refusal) or a read nothing
 *  answered. Never a change nothing answered, which may have gone through, nor a refusal with its reason. */
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
