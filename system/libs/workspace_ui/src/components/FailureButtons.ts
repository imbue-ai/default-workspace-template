import m from "mithril";
import type { ReportedFailure, ShellConnection } from "../app_contract";
import { REPORT_A_BUG_LABEL } from "../models/failure";
import { Button } from "./Button";
import type { ButtonOptions, ButtonVariant } from "./Button";

/**
 * The button a failure in a framed app page offers beside its sentence, which goes through the shell that frames the
 * page: "Report a bug" opens Imbue Studio's bug report with the failure attached. It renders nothing where it could
 * not work, so a surface draws it unconditionally; it reads the connection at render time, so the page redraws when a
 * handshake arrives (as every page does).
 */

interface FailureButtonOptions extends Pick<ButtonOptions, "sm" | "extra"> {
  readonly variant?: ButtonVariant;
}

export interface ReportBugButtonAttrs extends FailureButtonOptions {
  /** The page's connection; null before it connects. */
  readonly connection: Pick<ShellConnection, "canReportBug" | "reportBug"> | null;
  /** The failure on screen: its sentence and what its "See details" holds, which the report carries. */
  readonly failure: ReportedFailure;
}

/** Nothing unless the shell's handshake said Imbue Studio opens a bug report here. */
export const ReportBugButton: m.Component<ReportBugButtonAttrs> = {
  view(vnode) {
    const { connection, failure, variant, sm, extra } = vnode.attrs;
    if (connection === null || !connection.canReportBug()) return null;
    return m(
      Button,
      { variant, sm, extra: `report-bug ${extra ?? ""}`.trim(), onclick: () => connection.reportBug(failure) },
      REPORT_A_BUG_LABEL,
    );
  },
};
