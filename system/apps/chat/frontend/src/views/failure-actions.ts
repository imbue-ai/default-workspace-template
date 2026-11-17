/**
 * "Report a bug" as an action of a notice dialog, whose buttons are data rather than components: the same button
 * ``ReportBugButton`` draws elsewhere, offered only where the shell's handshake said Imbue Studio opens a bug report.
 */

import type { NoticeAction } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { REPORT_A_BUG_LABEL } from "@imbue/workspace-ui/src/models/failure";
import { reportedFailureOfLines } from "./failure-lines";
import type { FailureLines } from "./failure-lines";

/** "Report a bug" for the notice that shows ``lines``, carrying them; none when they ask for no bug report. */
export function reportBugActions(
  connection: Pick<ShellConnection, "canReportBug" | "reportBug"> | null,
  lines: FailureLines,
): NoticeAction[] {
  if (!lines.isReportable || connection === null || !connection.canReportBug()) return [];
  return [
    { label: REPORT_A_BUG_LABEL, isSecondary: true, run: () => connection.reportBug(reportedFailureOfLines(lines)) },
  ];
}
