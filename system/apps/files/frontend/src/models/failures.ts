/**
 * How Files tells a failed operation, as the workspace shell tells its own: a sentence of ours on screen, and the
 * status and the server's own text behind "See details". The sentence says what failed ("Couldn't move "notes.txt"
 * into "reports"") and then, when the server explained the refusal, why: in Files' own words for the refusals that
 * depend on which folder refused (the server's ``kind``), else in the server's sentence, which names only the cause.
 * A failure whose sentence asks for a bug report leaves a toast that stays, with "See details" and the remedies the
 * page's ``Toasts`` draws on a reportable toast ("Ask an agent to fix this" under a shell, then "Report a bug" where the
 * shell says Imbue Studio opens one); a full disk leaves one that stays with "Ask an agent to free up space"; any other
 * leaves a plain note, its retry being the control just used, with its details behind "See details" (opening them
 * keeps the note up).
 */

import { agentFixTask, assistRequest } from "@imbue/workspace-ui/src/app_contract";
import type {
  ReportedFailure,
  ShellConnection,
  StartOptions,
  StartOutcome,
} from "@imbue/workspace-ui/src/app_contract";
import { ASKING_AN_AGENT_LABEL } from "@imbue/workspace-ui/src/components/FailureButtons";
import { failureSentence, type Failure, type FailureMessage } from "@imbue/workspace-ui/src/models/failure";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import type { ToastAction, ToastQueue } from "@imbue/workspace-ui/src/models/toasts";

/** What a failure's buttons reach through the shell: an agent (in the chat about Files to fix a failure, in the main
 *  chat to free up space), and the bug report. */
export type FailureHost = Pick<
  ShellConnection,
  "hasShell" | "startWithText" | "askAgent" | "canReportBug" | "reportBug"
>;

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
  if (failure.cause !== "user_facing") {
    return { ...failureSentence(failure, whatFailed), isOutOfSpace: false };
  }
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

/** One operation's failures, of one item or many, as one toast. ``isAgentWithheld`` keeps the page's toasts from
 *  offering "Ask an agent to fix this" on it (a failed ask's own note). */
export function toastFailures(
  toasts: ToastQueue,
  failures: readonly FilesFailure[],
  host: FailureHost,
  redraw: () => void,
  isAgentWithheld = false,
): void {
  if (failures.length === 0) return;
  const message = [...new Set(failures.map((failure) => failure.message))].join("\n");
  const detailLines = failures.map((failure) => failure.details).filter((details) => details !== null);
  const details = detailLines.length === 0 ? null : detailLines.join("\n\n");
  const actions: ToastAction[] = [];
  // The main chat, which is already running: a chat made for the ask would need disk.
  if (host.hasShell() && failures.some((failure) => failure.isOutOfSpace)) {
    const task = assistRequest(agentFixTask({ whatFailed: message, details }));
    actions.push(
      askAgentAction(toasts, host, redraw, FREE_SPACE_LABEL, (options) => host.startWithText(task, options)),
    );
  }
  const isReportable = failures.some((failure) => failure.isReportable);
  if (!isReportable && actions.length === 0) {
    toasts.show(message, { isLasting: false, details });
    return;
  }
  toasts.show(message, { isLasting: true, details, actions, isReportable, isAgentWithheld });
}

/** What a toast's "Report a bug" does, decided as the toasts are drawn: null while the shell says Imbue Studio opens no
 *  bug report here. */
export function reportBugAction(host: FailureHost): ((failure: ReportedFailure) => void) | null {
  return host.canReportBug() ? (failure) => host.reportBug(failure) : null;
}

/**
 * A toast's button that asks an agent in the main chat ("Ask an agent to free up space"): "Asking an agent…" until the
 * shell says the chat opened, and a note saying why when it could not, which offers no agent in turn. An answer the
 * shell sends after a restart settled the ask as not answered replaces that note, unless the button was pressed again
 * since.
 */
function askAgentAction(
  toasts: ToastQueue,
  host: FailureHost,
  redraw: () => void,
  label: string,
  askThroughShell: (options: StartOptions) => Promise<StartOutcome>,
): ToastAction {
  let isAsking = false;
  let presses = 0;
  let toldId: number | null = null;
  const tell = (press: number, outcome: StartOutcome): void => {
    if (press !== presses) return;
    if (toldId !== null) toasts.dismiss(toldId);
    toldId = null;
    if (!outcome.isOpened) {
      toastFailures(toasts, [{ ...outcome.failure, isOutOfSpace: false }], host, redraw, true);
      const shown = toasts.current();
      toldId = shown.length === 0 ? null : shown[shown.length - 1].id;
    }
    redraw();
  };
  const ask = async (): Promise<void> => {
    presses += 1;
    const press = presses;
    isAsking = true;
    redraw();
    const outcome = await askThroughShell({ showsOutcome: true, onLateAnswer: (late) => tell(press, late) });
    if (press === presses) isAsking = false;
    tell(press, outcome);
  };
  return {
    label,
    run: () => void ask(),
    keepsToast: true,
    pending: { isPending: () => isAsking, label: ASKING_AN_AGENT_LABEL },
  };
}
