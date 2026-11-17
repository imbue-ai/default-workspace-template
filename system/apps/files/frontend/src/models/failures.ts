/**
 * How Files tells a failed operation, as the workspace shell tells its own: a sentence of ours on screen, and the
 * status and the server's own text behind "See details". The sentence says what failed ("Couldn't move "notes.txt"
 * into "reports"") and then, when the server explained the refusal, why: in Files' own words for the refusals that
 * depend on which folder refused (the server's ``kind``), else in the server's sentence, which names only the cause.
 * A failure whose sentence asks for a bug report leaves a toast that stays, with "See details" and, where the shell
 * says Imbue Studio opens one, "Report a bug"; a full disk leaves one that stays with "Ask an agent to free up space";
 * any other leaves a plain note, its retry being the control just used, and its details go to the log.
 */

import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { REPORT_A_BUG_LABEL, failureSentence, type FailureMessage } from "@imbue/workspace-ui/src/models/failure";
import type { Failure } from "@imbue/workspace-ui/src/models/failure";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import type { ToastAction, ToastQueue } from "@imbue/workspace-ui/src/models/toasts";

/** What a failure's toast reaches through the shell: an agent in the chat, and the bug report. */
export type FailureHost = Pick<ShellConnection, "hasShell" | "startWithText" | "canReportBug" | "reportBug">;

/** A failure as Files shows it. */
export interface FilesFailure extends FailureMessage {
  /** The workspace's disk is full, so the toast offers to ask an agent to free up space. */
  readonly isOutOfSpace: boolean;
}

/**
 * Where the operation that failed changes things, which decides how a refusal for want of write access is said:
 * `into-folder` makes something in the folder on show (create, upload), `item` changes an item where it is (save,
 * delete, rename), `move` takes an item into another folder, and `open` only reads.
 */
export type FailedOperation = "into-folder" | "item" | "move" | "open";

const NOT_WRITABLE_BY_OPERATION: Readonly<Record<FailedOperation, string>> = {
  "into-folder": "This folder is read-only.",
  item: "The folder it's in is read-only.",
  move: "You don't have permission to move it.",
  open: "You don't have permission to open it.",
};

const NOT_WRITABLE_KINDS: ReadonlySet<string> = new Set([
  "permission_denied",
  "read_only",
  "destination_not_writable",
  "source_not_writable",
]);

const FREE_SPACE_LABEL = "Ask an agent to free up space";
/** What an agent asked to free up space is told; it goes to the chat already running, as a new agent needs disk. */
const FREE_SPACE_REQUEST =
  "This workspace is out of disk space, so files can't be saved in it. Please free up some space.";

/** Whether a refusal's kind says the place could not be written: these, or a computer that shares it only to read. */
export function isNotWritable(kind: string | null): boolean {
  return kind !== null && (NOT_WRITABLE_KINDS.has(kind) || kind === "shared_read_only");
}

/** Why the server refused, in Files' words where the refusing folder matters, else in the server's sentence. */
function causeOf(failure: Failure & { cause: "user_facing" }, operation: FailedOperation): string {
  if (operation === "move" && failure.kind === "destination_not_writable") return "That folder is read-only.";
  if (operation === "move" && failure.kind === "source_not_writable") return "The folder it's in is read-only.";
  if (failure.kind !== null && NOT_WRITABLE_KINDS.has(failure.kind)) return NOT_WRITABLE_BY_OPERATION[operation];
  return failure.userFacingMessage;
}

/** A failed request in words, `whatFailed` naming what failed ("Couldn't delete "notes.md""). */
export function toldFailure(error: unknown, whatFailed: string, operation: FailedOperation): FilesFailure {
  const failure = describeRequestFailure(error);
  if (failure.cause !== "user_facing") return { ...failureSentence(failure, whatFailed), isOutOfSpace: false };
  return {
    message: `${whatFailed}. ${causeOf(failure, operation)}`,
    details: failure.details,
    isReportable: failure.isReportable,
    isOutOfSpace: failure.kind === "out_of_space",
  };
}

/** A refusal Files words itself before asking the server (a name already taken, a move it cannot make). */
export function plainFailure(message: string): FilesFailure {
  return { message, details: null, isReportable: false, isOutOfSpace: false };
}

/** One operation's failures, of one item or many, as one toast. */
export function toastFailures(toasts: ToastQueue, failures: readonly FilesFailure[], host: FailureHost): void {
  if (failures.length === 0) return;
  const message = [...new Set(failures.map((failure) => failure.message))].join("\n");
  const detailLines = failures.map((failure) => failure.details).filter((details) => details !== null);
  const details = detailLines.length === 0 ? null : detailLines.join("\n\n");
  const actions: ToastAction[] = [];
  if (failures.some((failure) => failure.isOutOfSpace) && host.hasShell()) {
    actions.push({ label: FREE_SPACE_LABEL, run: () => host.startWithText(FREE_SPACE_REQUEST) });
  }
  const isReportable = failures.some((failure) => failure.isReportable);
  if (isReportable && host.canReportBug()) {
    actions.push({ label: REPORT_A_BUG_LABEL, run: () => host.reportBug() });
  }
  if (!isReportable && actions.length === 0) {
    // A plain note has no room for the details, so they go to the log rather than nowhere.
    if (details !== null) console.warn(`[files] ${message}`, details);
    toasts.show(message);
    return;
  }
  toasts.show(message, { isLasting: true, details, actions });
}
