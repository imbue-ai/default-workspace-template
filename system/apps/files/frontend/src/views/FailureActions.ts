/**
 * The buttons under a failure Files shows in place of what it could not load (a folder's listing, a file's text):
 * whatever the surface leads with (Try again), then what a failure that owes help offers through the shell
 * (workspace_ui's ``FailureRemedies``: "Ask an agent to fix this", or "Report a bug" where no agent can be asked).
 */

import m from "mithril";

import { FailureRemedies } from "@imbue/workspace-ui/src/components/FailureButtons";

import type { FailureHost, FilesFailure } from "../models/failures";

export interface FailureActionsAttrs {
  readonly host: FailureHost;
  readonly failure: FilesFailure;
  /** What comes before the shell's buttons. */
  readonly leading?: m.Children;
  /** Whether the surface centres what it says, so the buttons and a refused ask under them are centred too. */
  readonly isCentred: boolean;
  readonly extra?: string;
}

const CENTRED =
  "justify-center [&>.ask-agent-failure]:flex [&>.ask-agent-failure]:flex-col [&>.ask-agent-failure]:items-center";

/** A press, and an ask the shell refused, belong to the failure they were made about: the same path failing the same
 *  way (its details name the path), however often it is read again. The refused ask is said under the buttons, and
 *  the one Report a bug files it with the failure. */
export const FailureActions: m.Component<FailureActionsAttrs> = {
  view({ attrs: { host, failure, leading, isCentred, extra } }) {
    const isRemedyOffered = failure.isHelpOwed && (host.hasShell() || host.canReportBug());
    if (leading === undefined && !isRemedyOffered) return null;
    const failureKey = JSON.stringify([failure.message, failure.details]);
    return m(
      "div.files-failure-actions",
      {
        class: ["flex flex-wrap gap-2 [&>.ask-agent-failure]:order-last", isCentred ? CENTRED : "", extra ?? ""]
          .join(" ")
          .trim(),
      },
      [
        m.fragment({ key: "leading" }, [leading]),
        m.fragment({ key: failureKey }, [m(FailureRemedies, { connection: host, failure, sm: true })]),
      ],
    );
  },
};
