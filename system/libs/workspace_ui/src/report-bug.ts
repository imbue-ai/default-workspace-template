/**
 * "Report a bug": the button a failure whose sentence asks for one carries. It opens Imbue Studio's bug report (its
 * Get help page) over this workspace, so a surface offers it only where ``canReportBug`` says the embedder opens one
 * (``opensHelp``); the sentence still says "report a bug" everywhere.
 */

import { OPEN_HELP } from "@minds/embed-contract";
import { embedderOpensHelp, sendToEmbedder } from "./embed";

export const REPORT_A_BUG_LABEL = "Report a bug";

export function openBugReport(): void {
  sendToEmbedder(OPEN_HELP);
}

/** Whether a "Report a bug" button would do anything here. */
export function canReportBug(): boolean {
  return embedderOpensHelp();
}
