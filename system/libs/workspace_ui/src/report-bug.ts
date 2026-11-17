/**
 * "Report a bug" in the shell, the one page the embedder hears directly: it opens Imbue Studio's bug report (its Get
 * help page) over this workspace, with the failure it is about attached. Only offered where the embedder's
 * capabilities said ``opensHelp``; an app page asks the shell through its connection instead (``canReportBug``,
 * ``reportBug``, ``ReportBugButton``).
 */

import { OPEN_HELP } from "@minds/embed-contract";
import { bugReportText } from "./app_contract";
import type { ReportedFailure } from "./app_contract";
import { MAX_HELP_ERROR_DETAILS_LENGTH, sendToEmbedder } from "./embed";

const TRUNCATED_MARK = "\n[...]";

/** ``text`` cut to what the embedder takes, its start kept: the sentence and the first lines say the most. */
function cappedErrorDetails(text: string): string {
  if (text.length <= MAX_HELP_ERROR_DETAILS_LENGTH) return text;
  return `${text.slice(0, MAX_HELP_ERROR_DETAILS_LENGTH - TRUNCATED_MARK.length)}${TRUNCATED_MARK}`;
}

/** Open the bug report about ``failure``; null opens it with nothing attached (a page that sent no failure). */
export function openBugReport(failure: ReportedFailure | null): void {
  sendToEmbedder(OPEN_HELP, failure === null ? {} : { errorDetails: cappedErrorDetails(bugReportText(failure)) });
}
