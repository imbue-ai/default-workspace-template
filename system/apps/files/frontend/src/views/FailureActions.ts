/**
 * The buttons under a failure Files shows in place of what it could not load (a folder's listing, a file's text):
 * whatever the surface leads with (Try again), then, through the shell, "Ask an agent to fix this" for a failure
 * nothing explained and "Report a bug" for a sentence that asks for one.
 */

import m from "mithril";

import { withAskFailure, type ShownFailure } from "@imbue/workspace-ui/src/app_contract";
import { AskAgentToFixButton, ReportBugButton } from "@imbue/workspace-ui/src/components/FailureButtons";

import type { FailureHost, FilesFailure } from "../models/failures";

export interface FailureActionsAttrs {
  readonly host: FailureHost;
  readonly failure: FilesFailure;
  /** The folder or file the failure is about. */
  readonly path: string;
  /** What comes before the shell's buttons. */
  readonly leading?: m.Children;
  /** Whether the surface centres what it says, so the buttons and a refused ask under them are centred too. */
  readonly isCentred: boolean;
  readonly extra?: string;
}

const CENTRED =
  "justify-center [&>.ask-agent-failure]:flex [&>.ask-agent-failure]:flex-col [&>.ask-agent-failure]:items-center";

/** A press, and an ask the shell refused, belong to the failure they were made about: the same path failing the same
 *  way, however often it is read again. The refused ask is said under the buttons, and the one Report a bug files it
 *  with the failure. */
export const FailureActions: m.ClosureComponent<FailureActionsAttrs> = () => {
  let shownFailureKey: string | null = null;
  let askFailure: ShownFailure | null = null;
  return {
    view({ attrs: { host, failure, path, leading, isCentred, extra } }) {
      const failureKey = JSON.stringify([path, failure.message, failure.details]);
      if (failureKey !== shownFailureKey) {
        shownFailureKey = failureKey;
        askFailure = null;
      }
      const isAgentOffered = failure.agentFix !== null && host.hasShell();
      const isReportOffered = failure.isReportable && host.canReportBug();
      if (leading === undefined && !isAgentOffered && !isReportOffered) return null;
      const reported = withAskFailure({ message: failure.message, details: failure.details }, askFailure);
      return m(
        "div.files-failure-actions",
        {
          class: ["flex flex-wrap gap-2 [&>.ask-agent-failure]:order-last", isCentred ? CENTRED : "", extra ?? ""]
            .join(" ")
            .trim(),
        },
        [
          m.fragment({ key: "leading" }, [leading]),
          m.fragment({ key: failureKey }, [
            failure.agentFix === null
              ? null
              : m(AskAgentToFixButton, {
                  connection: host,
                  failure: failure.agentFix,
                  sm: true,
                  pageReportBug: { onFailureChange: (next) => (askFailure = next) },
                }),
            failure.isReportable ? m(ReportBugButton, { connection: host, failure: reported, sm: true }) : null,
          ]),
        ],
      );
    },
  };
};
