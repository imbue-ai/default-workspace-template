/**
 * "Report a bug" in the shell, the one page the embedder hears directly: it opens Imbue Studio's bug report (its Get
 * help page) over this workspace. Only offered where the embedder's capabilities said ``opensHelp``; an app page asks
 * the shell through its connection instead (``canReportBug``, ``reportBug``, ``ReportBugButton``).
 */

import { OPEN_HELP } from "@minds/embed-contract";
import { sendToEmbedder } from "./embed";

export function openBugReport(): void {
  sendToEmbedder(OPEN_HELP);
}
