/**
 * What the chat page says about a failed request: the sentences on screen and what goes behind "See details". Each
 * surface names what failed and ``failureSentence`` words it; a surface whose request nothing answered may say what
 * that leaves unknown in its own words ("can't tell whether the agent stopped") in place of the general sentence.
 */

import { failureSentence } from "@imbue/workspace-ui/src/models/failure";
import type { Failure, RequestKind } from "@imbue/workspace-ui/src/models/failure";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";

export interface FailureLines {
  /** The paragraphs on screen, in order; empty entries are dropped. */
  readonly body: readonly (string | null)[];
  /** What goes behind "See details"; null when there is nothing beyond the body. */
  readonly details: string | null;
  /** The body asks for a bug report, so the surface offers "Report a bug". */
  readonly isReportable: boolean;
}

export interface FailureLinesOptions {
  readonly requestKind?: RequestKind;
  /** The surface's own sentence for a request nothing answered. */
  readonly notAnswered?: string;
}

/** ``error`` worded by ``failureSentence`` with ``whatFailed`` as its stem, or ``notAnswered`` when nothing answered. */
export function plainFailureLines(
  error: unknown,
  whatFailed: string,
  options: FailureLinesOptions = {},
): FailureLines {
  return plainFailureLinesOf(describeRequestFailure(error, options.requestKind ?? "change"), whatFailed, options);
}

/** ``plainFailureLines`` for a failure already read. */
export function plainFailureLinesOf(
  failure: Failure,
  whatFailed: string,
  options: FailureLinesOptions = {},
): FailureLines {
  if (failure.cause === "not_answered" && options.notAnswered !== undefined) {
    return { body: [options.notAnswered], details: failure.details, isReportable: false };
  }
  const told = failureSentence(failure, whatFailed);
  return { body: [told.message], details: told.details, isReportable: told.isReportable };
}

/** The non-empty ``lines`` of a "See details" text, joined; null when every one is empty. */
export function joinDetailLines(lines: readonly (string | null)[]): string | null {
  const kept = lines.map((line) => (line ?? "").trim()).filter((line) => line !== "");
  return kept.length === 0 ? null : kept.join("\n");
}

/** The one-paragraph sentence of ``plainFailureLines``, for a surface with room for a single line. */
export function plainFailureSentence(lines: FailureLines): string {
  return lines.body.filter((line): line is string => line !== null && line !== "").join(" ");
}
