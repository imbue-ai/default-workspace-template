/**
 * Failure helpers for the frontend tests: a refusal as the REST helper throws it, and a dialog's failed save worded
 * as the store words it.
 */

import { failureSentence, readFailure } from "@imbue/workspace-ui/src/models/failure";
import { HttpError } from "@imbue/workspace-ui/src/models/http";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import { isRetryable } from "../model/failures";
import type { FailureNotice } from "../model/failures";

/** The error the REST helper throws for ``status`` answered with ``body`` to a change. */
export function refusedWith(status: number, body: unknown, rawText: string | null = null): HttpError {
  return new HttpError(readFailure({ status, body, rawText, requestKind: "change" }));
}

/** A failed save as the store's ``changeFailureNotice`` words it for a dialog where no agent can be asked: the
 *  sentence, with Try again where the failure is one to try again, and a bug report offered where help is owed. */
export function wordFailureAsTheStoreDoes(whatFailed: string, error: unknown, retry: () => void): FailureNotice {
  const failure = describeRequestFailure(error);
  const actions = isRetryable(failure) ? [{ label: "Try again", run: retry }] : [];
  const told = failureSentence(failure, whatFailed);
  return { ...told, isBugReportOffered: told.isHelpOwed, actions };
}
