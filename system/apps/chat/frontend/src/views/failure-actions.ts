/**
 * "Report a bug" as an action of a notice dialog, whose buttons are data rather than components: the same button
 * ``ReportBugButton`` draws elsewhere, offered only where the shell's handshake said Imbue Studio opens a bug report.
 */

import type { NoticeAction } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { REPORT_A_BUG_LABEL } from "@imbue/workspace-ui/src/models/failure";

export function reportBugActions(
  connection: Pick<ShellConnection, "canReportBug" | "reportBug"> | null,
  isReportable: boolean,
): NoticeAction[] {
  if (!isReportable || connection === null || !connection.canReportBug()) return [];
  return [{ label: REPORT_A_BUG_LABEL, isSecondary: true, run: () => connection.reportBug() }];
}
