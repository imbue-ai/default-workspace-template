import m from "mithril";
import { askAgentToFix } from "../app_contract";
import type { AgentFixRequest, ShellConnection } from "../app_contract";
import { REPORT_A_BUG_LABEL } from "../models/failure";
import { Button } from "./Button";
import type { ButtonOptions, ButtonVariant } from "./Button";

/**
 * The two buttons a failure in a framed app page offers beside its sentence, both of which go through the shell that
 * frames the page: "Ask an agent to fix this" starts a chat with the failure as its task, and "Report a bug" opens
 * Imbue Studio's bug report. Each renders nothing where it could not work, so a surface draws both unconditionally;
 * it reads the connection at render time, so the page redraws when a handshake arrives (as every page does).
 */

export const ASK_AGENT_TO_FIX_LABEL = "Ask an agent to fix this";

interface FailureButtonOptions extends Pick<ButtonOptions, "sm" | "extra"> {
  readonly variant?: ButtonVariant;
}

export interface AskAgentToFixButtonAttrs extends FailureButtonOptions {
  /** The page's connection; null before it connects. */
  readonly connection: Pick<ShellConnection, "hasShell" | "startWithText"> | null;
  readonly failure: AgentFixRequest;
}

/** Nothing without a shell (a page opened on its own, a share visitor holding only this app): no chat can start. */
export const AskAgentToFixButton: m.Component<AskAgentToFixButtonAttrs> = {
  view(vnode) {
    const { connection, failure, variant, sm, extra } = vnode.attrs;
    if (connection === null || !connection.hasShell()) return null;
    return m(
      Button,
      {
        variant,
        sm,
        extra: `ask-agent-to-fix ${extra ?? ""}`.trim(),
        onclick: () => askAgentToFix(connection, failure),
      },
      ASK_AGENT_TO_FIX_LABEL,
    );
  },
};

export interface ReportBugButtonAttrs extends FailureButtonOptions {
  /** The page's connection; null before it connects. */
  readonly connection: Pick<ShellConnection, "canReportBug" | "reportBug"> | null;
}

/** Nothing unless the shell's handshake said Imbue Studio opens a bug report here. */
export const ReportBugButton: m.Component<ReportBugButtonAttrs> = {
  view(vnode) {
    const { connection, variant, sm, extra } = vnode.attrs;
    if (connection === null || !connection.canReportBug()) return null;
    return m(
      Button,
      { variant, sm, extra: `report-bug ${extra ?? ""}`.trim(), onclick: () => connection.reportBug() },
      REPORT_A_BUG_LABEL,
    );
  },
};
