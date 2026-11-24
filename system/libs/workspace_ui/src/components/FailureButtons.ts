import m from "mithril";
import { askAgentToFix, withAskFailure } from "../app_contract";
import type { AgentFixRequest, ReportedFailure, ShellConnection, ShownFailure, StartOutcome } from "../app_contract";
import { REPORT_A_BUG_LABEL } from "../models/failure";
import { Button } from "./Button";
import type { ButtonOptions, ButtonVariant } from "./Button";
import { SeeDetails } from "./SeeDetails";
import { TEXT_HELPER_SIZE } from "./typography";

/**
 * The buttons a failure in a framed app page offers beside its sentence, which go through the shell that frames the
 * page: "Ask an agent to fix this" sends the failure to an agent as its task, and "Report a bug" opens Imbue Studio's
 * bug report with the failure attached. Each renders nothing where it could not work; it reads the connection at
 * render time, so the page redraws when a handshake arrives (as every page does). ``FailureRemedies`` draws what a
 * failure that owes help gets by default: the agent, and the bug report only where no agent can be asked or the ask
 * was refused.
 */

export const ASK_AGENT_TO_FIX_LABEL = "Ask an agent to fix this";
export const ASKING_AN_AGENT_LABEL = "Asking an agent…";
/** What a refused ask offers: the same request left unsent in a chat's message box, for the person to send. */
export const DRAFT_ASK_LABEL = "Draft it in a chat";

interface FailureButtonOptions extends Pick<ButtonOptions, "sm" | "extra"> {
  readonly variant?: ButtonVariant;
}

export interface AskAgentToFixButtonAttrs extends FailureButtonOptions {
  /** The page's connection; null before it connects. */
  readonly connection: Pick<ShellConnection, "hasShell" | "askAgent" | "canReportBug" | "reportBug"> | null;
  readonly failure: AgentFixRequest;
  /** The failure the page shows, when the ask is about one: the Report a bug under a refused ask then files both
   *  (``withAskFailure``), not the ask's failure alone. */
  readonly reported?: ReportedFailure;
  /** The page offers its own Report a bug beside this button: a refused ask then shows its sentence and details
   *  without a Report a bug of its own, and is handed to ``onFailureChange`` (null once it is gone) so the page's one
   *  report can carry it too (``withAskFailure``). */
  readonly pageReportBug?: { readonly onFailureChange: (failure: ShownFailure | null) => void };
}

/** Nothing without a shell (a page opened on its own, a share visitor holding only this app): nothing can carry the
 *  ask. Pressed, it reads "Asking an agent…" and stays disabled until the shell says the chat's window opened or the
 *  ask failed; a failure is shown under it, and the button is back for another try. A failure that owes help offers
 *  to draft the request in a chat instead, and Report a bug (unless the page reports bugs itself). An answer that
 *  comes after the press was settled as not answered replaces what is shown. */
export const AskAgentToFixButton: m.ClosureComponent<AskAgentToFixButtonAttrs> = () => {
  let isAsking = false;
  let failure: ShownFailure | null = null;
  let latestAsk = 0;
  let latestAttrs: AskAgentToFixButtonAttrs | null = null;

  function setFailure(next: ShownFailure | null): void {
    if (next === failure) return;
    failure = next;
    latestAttrs?.pageReportBug?.onFailureChange(next);
  }

  function show(outcome: StartOutcome): void {
    setFailure(outcome.isOpened ? null : outcome.failure);
    m.redraw();
  }

  async function ask(attrs: AskAgentToFixButtonAttrs): Promise<void> {
    if (attrs.connection === null || isAsking) return;
    latestAsk += 1;
    const thisAsk = latestAsk;
    isAsking = true;
    setFailure(null);
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
    onremove() {
      latestAsk += 1;
      setFailure(null);
      latestAttrs = null;
    },
    view(vnode) {
      latestAttrs = vnode.attrs;
      const { connection, variant, sm, extra, reported, pageReportBug } = vnode.attrs;
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
              failure.isHelpOwed
                ? m("div", { class: "mt-1 flex flex-wrap justify-center gap-1" }, [
                    m(
                      Button,
                      {
                        sm: true,
                        extra: "draft-ask",
                        // The shell tells a draft that could not be made itself.
                        onclick: () =>
                          void askAgentToFix(connection, vnode.attrs.failure, { showsOutcome: true }, "draft"),
                      },
                      DRAFT_ASK_LABEL,
                    ),
                    pageReportBug === undefined
                      ? m(ReportBugButton, {
                          connection,
                          failure:
                            reported === undefined
                              ? { message: failure.message, details: failure.details }
                              : withAskFailure(reported, failure),
                          sm: true,
                        })
                      : null,
                  ])
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

export interface FailureRemediesAttrs extends FailureButtonOptions {
  /** The page's connection; null before it connects. */
  readonly connection: Pick<ShellConnection, "hasShell" | "askAgent" | "canReportBug" | "reportBug"> | null;
  /** The failure on screen. */
  readonly failure: ShownFailure;
  /** Offer no agent: the ask would go through what failed (the chat app's own server not answering). */
  readonly isAgentWithheld?: boolean;
}

/** What a failure that owes help offers by default: "Ask an agent to fix this", whose refused ask offers the draft
 *  and a "Report a bug" that files both failures, or "Report a bug" in its place where no agent can be asked (the
 *  surface withholds it, or no shell frames the page). Nothing for a failure that owes none, whose surface offers
 *  its own remedy. */
export const FailureRemedies: m.Component<FailureRemediesAttrs> = {
  view(vnode) {
    const { connection, failure, isAgentWithheld, variant, sm, extra } = vnode.attrs;
    if (!failure.isHelpOwed) return null;
    const options = { variant, sm, extra };
    const reported: ReportedFailure = { message: failure.message, details: failure.details };
    if (isAgentWithheld !== true && connection !== null && connection.hasShell()) {
      return m(AskAgentToFixButton, {
        ...options,
        connection,
        failure: { whatFailed: failure.message, details: failure.details },
        reported,
      });
    }
    return m(ReportBugButton, { ...options, connection, failure: reported });
  },
};
