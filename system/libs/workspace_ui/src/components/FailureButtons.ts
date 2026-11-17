import m from "mithril";
import { askAgentToFix } from "../app_contract";
import type {
  AgentFixRequest,
  ReportedFailure,
  ShellConnection,
  ShownFailure,
  StartNewOutcome,
} from "../app_contract";
import { REPORT_A_BUG_LABEL } from "../models/failure";
import { Button } from "./Button";
import type { ButtonOptions, ButtonVariant } from "./Button";
import { SeeDetails } from "./SeeDetails";
import { TEXT_HELPER_SIZE } from "./typography";

/**
 * The two buttons a failure in a framed app page offers beside its sentence, both of which go through the shell that
 * frames the page: "Ask an agent to fix this" starts a new chat with the failure as its task, and "Report a bug" opens
 * Imbue Studio's bug report with the failure attached. Each renders nothing where it could not work, so a surface
 * draws both unconditionally; it reads the connection at render time, so the page redraws when a handshake arrives (as
 * every page does).
 */

export const ASK_AGENT_TO_FIX_LABEL = "Ask an agent to fix this";
export const ASKING_AN_AGENT_LABEL = "Asking an agent…";

interface FailureButtonOptions extends Pick<ButtonOptions, "sm" | "extra"> {
  readonly variant?: ButtonVariant;
}

export interface AskAgentToFixButtonAttrs extends FailureButtonOptions {
  /** The page's connection; null before it connects. */
  readonly connection: Pick<ShellConnection, "hasShell" | "startNewWithText" | "canReportBug" | "reportBug"> | null;
  readonly failure: AgentFixRequest;
}

/** Nothing without a shell (a page opened on its own, a share visitor holding only this app): no chat can start.
 *  Pressed, it reads "Asking an agent…" and stays disabled until the shell says the chat's window opened or the start
 *  failed; a failure is shown under it, with Report a bug when it asks for one, and the button is back for another
 *  try. An answer that comes after the press was settled as not answered replaces what is shown. */
export const AskAgentToFixButton: m.ClosureComponent<AskAgentToFixButtonAttrs> = () => {
  let isAsking = false;
  let failure: ShownFailure | null = null;
  let latestAsk = 0;

  function show(outcome: StartNewOutcome): void {
    failure = outcome.isOpened ? null : outcome.failure;
    m.redraw();
  }

  async function ask(attrs: AskAgentToFixButtonAttrs): Promise<void> {
    if (attrs.connection === null || isAsking) return;
    latestAsk += 1;
    const thisAsk = latestAsk;
    isAsking = true;
    failure = null;
    const outcome = await askAgentToFix(attrs.connection, attrs.failure, {
      showsOutcome: true,
      onLateAnswer: (late) => {
        if (thisAsk === latestAsk && !isAsking) show(late);
      },
    });
    isAsking = false;
    show(outcome);
  }

  return {
    view(vnode) {
      const { connection, variant, sm, extra } = vnode.attrs;
      if (connection === null || !connection.hasShell()) return null;
      return [
        m(
          Button,
          {
            variant,
            sm,
            extra: `ask-agent-to-fix ${extra ?? ""}`.trim(),
            disabled: isAsking,
            onclick: () => void ask(vnode.attrs),
          },
          isAsking ? ASKING_AN_AGENT_LABEL : ASK_AGENT_TO_FIX_LABEL,
        ),
        failure === null
          ? null
          : m("div", { class: `ask-agent-failure ${TEXT_HELPER_SIZE} basis-full`, role: "alert" }, [
              m("p", { class: "ask-agent-failure-message" }, failure.message),
              m(SeeDetails, { details: failure.details }),
              failure.isReportable
                ? m(ReportBugButton, {
                    connection,
                    failure: { message: failure.message, details: failure.details },
                    sm: true,
                    extra: "mt-1",
                  })
                : null,
            ]),
      ];
    },
  };
};

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
