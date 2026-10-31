/**
 * What a person is told when a request to the workspace fails. ``readFailure`` reads what went wrong from what came
 * back (a sentence the server wrote for people, a reason of its own, no answer at all, or nothing to go on), and
 * ``failureSentence`` is the one place that becomes words: a sentence of ours on screen, and everything underneath it
 * (a status, the server's own text) behind "See details" rather than on the visible line.
 */

/** Whether a failed request only read something (nothing it did can have changed) or asked for a change, which an
 *  unanswered request may have carried out anyway. */
export type RequestKind = "read" | "change";

/** What a surface knows about a failure: the shared fields, and what was learned about its cause. */
export type Failure = FailureFields &
  (
    | {
        /** The server wrote a sentence for people (``user_facing_message``), shown as written. */
        readonly cause: "user_facing";
        readonly userFacingMessage: string;
        /** The server's developer text beside its sentence, when it gave one. */
        readonly reason: string | null;
        /** The server said its sentence asks for a bug report (``reportable: true``). */
        readonly isReportable: boolean;
      }
    | {
        /** The server (or the code that threw) gave a reason of its own (``detail`` or ``error``), developer text
         *  that goes behind "See details". */
        readonly cause: "reason";
        readonly reason: string;
      }
    | {
        /** Nothing answered: a dropped connection, a timeout, or a gateway status with no reason of the server's. */
        readonly cause: "not_answered";
      }
    | {
        /** An answer that says nothing about why. */
        readonly cause: "no_reason";
      }
  );

interface FailureFields {
  readonly requestKind: RequestKind;
  /** The HTTP status; 0 when nothing answered; null for an error no request answered with (one the code threw). */
  readonly status: number | null;
  /** What goes behind "See details"; null when there is nothing beyond the sentence. */
  readonly details: string | null;
  /** The route's name for the case (its body's ``kind``), for a surface that offers different controls per case. */
  readonly kind: string | null;
  /** The JSON object the server answered, for a caller that reads a field of its own; null when there was none. */
  readonly body: Readonly<Record<string, unknown>> | null;
}

/** A failure as a surface shows it. */
export interface FailureMessage {
  readonly message: string;
  /** What goes behind "See details"; null when there is nothing beyond the sentence. */
  readonly details: string | null;
  /** The sentence asks for a bug report, so the surface offers "Report a bug" (where the embedder can open one). */
  readonly isReportable: boolean;
}

/** What Imbue Studio's forward proxy and the workspace's own front door answer when the server behind them did not.
 *  Neither tells whether the request reached the server first. */
const GATEWAY_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);

const READ_NOT_ANSWERED = "The workspace didn't answer. Try again in a moment.";
// Never "nothing was changed": a gateway status can arrive after the request was carried out, and a connection that
// dropped after the request was sent looks the same as one that never opened.
const CHANGE_NOT_ANSWERED = "The workspace didn't answer, so Imbue Studio can't tell whether this went through.";

/** What failed, for a surface that has not said in its own words yet. */
export const DEFAULT_WHAT_FAILED = "The workspace couldn't do that";

const REPORT_A_BUG_ENDING = "Try again. If this persists, report a bug.";

/** The button a sentence that asks for a bug report carries, where Imbue Studio can open one. */
export const REPORT_A_BUG_LABEL = "Report a bug";

// How much of a body that is not a reason (an error page, a proxy's plain text) "See details" quotes.
const RAW_BODY_LIMIT = 2000;

/** The sentence for a request nothing answered (a gateway status, a dropped connection, a timeout). */
export function notAnsweredSentence(kind: RequestKind): string {
  return kind === "read" ? READ_NOT_ANSWERED : CHANGE_NOT_ANSWERED;
}

/** The fallback for a failure with no explanation of its own: what failed, then the bug-report ending. */
export function fallbackSentence(whatFailed: string): string {
  return `${whatFailed}. ${REPORT_A_BUG_ENDING}`;
}

/** The non-empty lines of a "See details" text, joined; null when every one is empty. */
function joinDetails(lines: readonly (string | null | undefined)[]): string | null {
  const kept = lines.map((line) => (line ?? "").trim()).filter((line) => line !== "");
  return kept.length === 0 ? null : kept.join("\n");
}

/** ``value`` trimmed when it is a string with something in it; null otherwise. */
export function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

/** What came back from a failed request. */
export interface FailedAnswer {
  /** The HTTP status, or 0 when nothing answered. */
  readonly status: number;
  /** The parsed body, whatever it was (null when it was not JSON). */
  readonly body: unknown;
  /** The body's raw text, or what the transport said when nothing answered; quoted behind "See details" when the
   *  body gave no reason. */
  readonly rawText: string | null;
  readonly requestKind: RequestKind;
}

/**
 * What a failed request's answer says about why: a non-empty ``user_facing_message``, else a non-empty ``detail``
 * (or ``error``), else no answer (status 0 or a gateway status), else nothing. A gateway status that carries a JSON
 * reason is the server's own answer, not the proxy's: Imbue Studio's forward proxy answers its own 502, 503 and 504
 * as plain text or an HTML page.
 */
export function readFailure(answer: FailedAnswer): Failure {
  const { status, rawText, requestKind } = answer;
  const body =
    typeof answer.body === "object" && answer.body !== null
      ? (answer.body as Readonly<Record<string, unknown>>)
      : null;
  const userFacing = nonEmptyString(body?.user_facing_message);
  const reason = nonEmptyString(body?.detail) ?? nonEmptyString(body?.error);
  const fields: FailureFields = {
    requestKind,
    status,
    details: joinDetails([
      status === 0 ? null : `HTTP ${status}`,
      reason,
      nonEmptyString(body?.details),
      userFacing === null && reason === null ? rawText?.slice(0, RAW_BODY_LIMIT) : null,
    ]),
    kind: nonEmptyString(body?.kind),
    body,
  };
  if (userFacing !== null) {
    return {
      ...fields,
      cause: "user_facing",
      userFacingMessage: userFacing,
      reason,
      isReportable: body?.reportable === true,
    };
  }
  if (reason !== null) return { ...fields, cause: "reason", reason };
  if (status === 0 || GATEWAY_STATUSES.has(status)) return { ...fields, cause: "not_answered" };
  return { ...fields, cause: "no_reason" };
}

/**
 * ``failure`` in words, ``whatFailed`` being a sentence stem naming what failed ("This window couldn't be closed").
 * The server's sentence for people stands as written; no answer says only that, whatever failed; a refusal (a 4xx)
 * with a reason names what failed and puts the reason behind "See details"; anything else is the fallback, which asks
 * for a bug report.
 */
export function failureSentence(failure: Failure, whatFailed: string = DEFAULT_WHAT_FAILED): FailureMessage {
  const { details } = failure;
  switch (failure.cause) {
    case "user_facing":
      return { message: failure.userFacingMessage, details, isReportable: failure.isReportable };
    case "not_answered":
      return { message: notAnsweredSentence(failure.requestKind), details, isReportable: false };
    case "reason":
      if (failure.status !== null && failure.status >= 400 && failure.status < 500) {
        return { message: `${whatFailed}.`, details, isReportable: false };
      }
      return { message: fallbackSentence(whatFailed), details, isReportable: true };
    case "no_reason":
      return { message: fallbackSentence(whatFailed), details, isReportable: true };
  }
}

/**
 * What ``failure`` says in the server's own words, for a surface not yet worded with ``failureSentence`` and for the
 * log: the server's reason (developer text), else its sentence for people, else ``failureSentence``'s sentence for a
 * failure that carries neither. An ``HttpError``'s ``message``.
 */
export function failureText(failure: Failure): string {
  switch (failure.cause) {
    case "user_facing":
      return failure.reason ?? failure.userFacingMessage;
    case "reason":
      return failure.reason;
    case "not_answered":
    case "no_reason":
      return failureSentence(failure).message;
  }
}
