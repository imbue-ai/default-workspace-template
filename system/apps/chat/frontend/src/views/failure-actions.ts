/**
 * "Ask an agent to fix this" and "Report a bug" as actions of a notice dialog, whose buttons are data rather than
 * components: the pair ``FailureRemedies`` draws elsewhere. The notice makes the ask and shows how it went; the report
 * is offered only where the shell's handshake said Imbue Studio opens a bug report.
 */

import type { NoticeAction } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { ASKING_AN_AGENT_LABEL, ASK_AGENT_TO_FIX_LABEL } from "@imbue/workspace-ui/src/components/FailureButtons";
import { REPORT_A_BUG_LABEL } from "@imbue/workspace-ui/src/models/failure";
import { reportedFailureOfLines } from "./failure-lines";
import type { FailureLines } from "./failure-lines";

/** A notice's "Ask an agent to fix this": the press, and whether the ask it made is still under way. */
export interface NoticeAsk {
  readonly isAsking: boolean;
  readonly run: () => void;
}

/** What a notice offers for the failure ``lines`` tell: "Ask an agent to fix this" (``ask``; null where the notice
 *  offers none: a failure that asks for no bug report and leaves a way out, or one whose ask would go through what
 *  failed or that nothing could carry), then "Report a bug" when the failure asks for one. */
export function failureRemedyActions(
  connection: Pick<ShellConnection, "canReportBug" | "reportBug"> | null,
  lines: FailureLines,
  ask: NoticeAsk | null,
): NoticeAction[] {
  return [
    ...(ask === null
      ? []
      : [
          {
            label: ask.isAsking ? ASKING_AN_AGENT_LABEL : ASK_AGENT_TO_FIX_LABEL,
            tooltip: "Sends what failed to an agent, and shows its chat",
            isSecondary: true,
            run: ask.run,
          },
        ]),
    ...reportBugActions(connection, lines),
  ];
}

/** "Report a bug" for the notice that shows ``lines``, carrying them; none when they ask for no bug report. */
function reportBugActions(
  connection: Pick<ShellConnection, "canReportBug" | "reportBug"> | null,
  lines: FailureLines,
): NoticeAction[] {
  if (!lines.isReportable || connection === null || !connection.canReportBug()) return [];
  return [
    { label: REPORT_A_BUG_LABEL, isSecondary: true, run: () => connection.reportBug(reportedFailureOfLines(lines)) },
  ];
}
