/**
 * How Files tells a failed operation, as the workspace shell tells its own: a sentence of ours on screen
 * (`failureSentence`), and the status and the server's own text behind "See details". A failure whose sentence asks
 * for a bug report leaves a toast that stays, with "See details" and, where the shell says Imbue Studio opens one,
 * "Report a bug"; any other leaves a plain note, its retry being the control just used, and its details go to the log.
 */

import { REPORT_A_BUG_LABEL, failureSentence, type FailureMessage } from "@imbue/workspace-ui/src/models/failure";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import type { ToastQueue } from "@imbue/workspace-ui/src/models/toasts";

/** The shell's bug report, as a page reaches it through its connection. */
export interface BugReporter {
  canReportBug(): boolean;
  reportBug(): void;
}

/** A failed request in words, `whatFailed` naming what failed ("Couldn't delete "notes.md""). */
export function toldFailure(error: unknown, whatFailed: string): FailureMessage {
  return failureSentence(describeRequestFailure(error), whatFailed);
}

/** A refusal Files words itself before asking the server (a name already taken, a move it cannot make). */
export function plainFailure(message: string): FailureMessage {
  return { message, details: null, isReportable: false };
}

/** One operation's failures, of one item or many, as one toast. */
export function toastFailures(toasts: ToastQueue, failures: readonly FailureMessage[], reporter: BugReporter): void {
  if (failures.length === 0) return;
  const message = [...new Set(failures.map((failure) => failure.message))].join("\n");
  const detailLines = failures.map((failure) => failure.details).filter((details) => details !== null);
  const details = detailLines.length === 0 ? null : detailLines.join("\n\n");
  if (!failures.some((failure) => failure.isReportable)) {
    // A plain note has no room for the details, so they go to the log rather than nowhere.
    if (details !== null) console.warn(`[files] ${message}`, details);
    toasts.show(message);
    return;
  }
  toasts.show(message, {
    isLasting: true,
    details,
    actions: reporter.canReportBug() ? [{ label: REPORT_A_BUG_LABEL, run: () => reporter.reportBug() }] : [],
  });
}
