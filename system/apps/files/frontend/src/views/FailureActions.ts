/**
 * The buttons under a failure Files shows in place of what it could not load (a folder's listing, a file's text):
 * whatever the surface leads with (Try again), then what a failure that asks for a bug report offers through the shell
 * (workspace_ui's ``FailureRemedies``: "Ask an agent to fix this", then "Report a bug").
 */

import m from "mithril";

import { FailureRemedies } from "@imbue/workspace-ui/src/components/FailureButtons";

import type { FailureHost, FilesFailure } from "../models/failures";

export interface FailureActionsAttrs {
  readonly host: FailureHost;
  readonly failure: FilesFailure;
  /** The folder or file the failure is about; null for one about no place (what your computers share). */
  readonly path: string | null;
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
 *  with the failure. The agent and the report are told the path, which the surface shows rather than says in its
 *  sentence. */
export const FailureActions: m.Component<FailureActionsAttrs> = {
  view({ attrs: { host, failure, path, leading, isCentred, extra } }) {
    const isRemedyOffered = failure.isReportable && (host.hasShell() || host.canReportBug());
    if (leading === undefined && !isRemedyOffered) return null;
    const failureKey = JSON.stringify([path, failure.message, failure.details]);
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
          m(FailureRemedies, {
            connection: host,
            failure: {
              message: failure.message,
              details: [path, failure.details].filter((part) => part !== null).join("\n"),
              isReportable: failure.isReportable,
            },
            sm: true,
          }),
        ]),
      ],
    );
  },
};
