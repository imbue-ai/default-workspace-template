/**
 * What a failure that owes help offers, as actions of a notice dialog, whose buttons are data rather than components:
 * what ``FailureRemedies`` draws elsewhere. "Ask an agent to fix this" where an agent can be asked, and "Report a bug"
 * only where none can or once the ask was refused with a failure that owes help itself, when the same request can
 * also be left as a draft in a chat. The notice makes the ask and shows how it went; the report is offered only where
 * the shell's handshake said Imbue Studio opens a bug report.
 */

import type { NoticeAction } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type { ShellConnection, ShownFailure } from "@imbue/workspace-ui/src/app_contract";
import {
  ASKING_AN_AGENT_LABEL,
  ASK_AGENT_TO_FIX_LABEL,
  DRAFT_ASK_LABEL,
} from "@imbue/workspace-ui/src/components/FailureButtons";
import { REPORT_A_BUG_LABEL } from "@imbue/workspace-ui/src/models/failure";
import { reportedFailureOfLines } from "./failure-lines";
import type { FailureLines } from "./failure-lines";

/** A notice's "Ask an agent to fix this": the press, whether the ask it made is still under way, and how the last
 *  one was refused. */
export interface NoticeAsk {
  readonly isAsking: boolean;
  readonly run: () => void;
  /** Why the last ask could not be sent; null before one was refused. */
  readonly refusal: ShownFailure | null;
  /** Leave the same request unsent in a chat's message box, for the person to send. */
  readonly draft: () => void;
}

/** What a notice offers for the failure ``lines`` tell (a refused ask's sentence among them) when it owes help: "Ask
 *  an agent to fix this" (``ask``; null where the notice withholds it, because the ask would go through what failed
 *  or nothing could carry it), then, once that ask was refused with a failure that owes help, "Draft it in a chat"
 *  and "Report a bug"; an ask refused for a reason that owes none (nothing answered, a view to stop first) is only
 *  asked again. With no agent to ask, "Report a bug" alone. Nothing for a failure that owes no help, whose notice
 *  offers its own remedy. */
export function failureRemedyActions(
  connection: Pick<ShellConnection, "canReportBug" | "reportBug"> | null,
  lines: FailureLines,
  ask: NoticeAsk | null,
): NoticeAction[] {
  if (!lines.isHelpOwed) return [];
  if (ask === null) return reportBugActions(connection, lines);
  const askAction: NoticeAction = {
    label: ask.isAsking ? ASKING_AN_AGENT_LABEL : ASK_AGENT_TO_FIX_LABEL,
    tooltip: "Sends what failed to an agent, and shows its chat",
    isSecondary: true,
    run: ask.run,
  };
  if (ask.refusal === null || !ask.refusal.isHelpOwed) return [askAction];
  return [
    askAction,
    {
      label: DRAFT_ASK_LABEL,
      tooltip: "Leaves the same request in a chat's message box, for you to send",
      isSecondary: true,
      run: ask.draft,
    },
    ...reportBugActions(connection, lines),
  ];
}

/** "Report a bug" for the notice that shows ``lines``, carrying them; none where no bug report can be opened. */
function reportBugActions(
  connection: Pick<ShellConnection, "canReportBug" | "reportBug"> | null,
  lines: FailureLines,
): NoticeAction[] {
  if (connection === null || !connection.canReportBug()) return [];
  return [
    { label: REPORT_A_BUG_LABEL, isSecondary: true, run: () => connection.reportBug(reportedFailureOfLines(lines)) },
  ];
}
