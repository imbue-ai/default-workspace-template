/**
 * How the desktop shows a failure on its own surfaces (a toast, a dialog's line, the launcher, the backdrop, a
 * window's placeholder): a sentence, what goes behind "See details", and its buttons. A failed request's sentence is
 * ``failureSentence``'s; the store adds the buttons, "Report a bug" among them where the embedder opens one.
 */

import { REPORT_A_BUG_LABEL } from "@imbue/workspace-ui/src/models/failure";
import type { Failure, FailureMessage } from "@imbue/workspace-ui/src/models/failure";
import type { AppRecord } from "./records";
import type { ToastAction } from "@imbue/workspace-ui/src/models/toasts";

/** A failure as a desktop surface shows it. */
export interface FailureNotice {
  readonly message: string;
  /** What goes behind "See details"; null when there is nothing beyond the sentence. */
  readonly details: string | null;
  /** Its buttons, in order ("Report a bug" last). */
  readonly actions: readonly ToastAction[];
  /** The sentence asks for a bug report, so a toast of it stays even where "Report a bug" cannot be offered. */
  readonly isReportable: boolean;
}

/** ``told`` as a surface shows it: ``actions``, then "Report a bug" where the sentence asks for one and a bug report
 *  can be opened (``reportBug`` is not null). */
export function noticeOf(
  told: FailureMessage,
  actions: readonly ToastAction[],
  reportBug: (() => void) | null,
): FailureNotice {
  const report = told.isReportable && reportBug !== null ? [{ label: REPORT_A_BUG_LABEL, run: reportBug }] : [];
  return { ...told, actions: [...actions, ...report] };
}

/** What the shell's refusals name with ``kind`` (desktop contracts.md, the error body), for the desktop to word
 *  with what only it holds. */
export type RefusalKind = "app_unavailable" | "launch_path_gone" | "window_gone" | "out_of_space" | "not_saved";

const REFUSAL_KINDS: ReadonlySet<string> = new Set<RefusalKind>([
  "app_unavailable",
  "launch_path_gone",
  "window_gone",
  "out_of_space",
  "not_saved",
]);

/** What a window, a shortcut, or a launcher row says of an app that is not running and that the workspace does not
 *  start on a request: one it does not run itself, or one it treats as part of how it runs. */
export function stoppedSentence(app: AppRecord): string {
  return app.program === "" ? `${app.display_name} isn't running.` : `${app.display_name} is not running right now.`;
}

/** The kind of refusal the shell answered ``failure`` with, or null for any other failure. */
export function refusalKindOf(failure: Failure): RefusalKind | null {
  return failure.kind !== null && REFUSAL_KINDS.has(failure.kind) ? (failure.kind as RefusalKind) : null;
}
