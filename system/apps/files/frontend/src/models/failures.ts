/**
 * How Files tells a failed operation, as the workspace shell tells its own: a sentence of ours on screen, and what was
 * tried, the status and the server's own text behind "See details". The sentence says what failed ("Couldn't move
 * "notes.txt" into "reports"") and then, when the server explained the refusal, why: in Files' own words for the
 * refusals whose words depend on what the person was doing (by the server's ``kind``), else in the server's sentence,
 * which names only the cause. An item the server says is gone is told as that alone. A failure that owes the person
 * help leaves a toast that stays, on which the page's ``Toasts`` draws "Ask an agent to fix this" under a shell
 * ("Report a bug" where only that can be opened), the toast carrying what the agent is asked; a full disk leaves one
 * that stays with "Ask an agent to free up space" instead; any other leaves a plain note, its retry being the control
 * just used, with its details behind "See details" (opening them keeps the note up).
 */

import { askAgentToFix } from "@imbue/workspace-ui/src/app_contract";
import type {
  AgentFixRequest,
  ReportedFailure,
  ShellConnection,
  StartOptions,
  StartOutcome,
} from "@imbue/workspace-ui/src/app_contract";
import { ASKING_AN_AGENT_LABEL, DRAFT_ASK_LABEL } from "@imbue/workspace-ui/src/components/FailureButtons";
import { failureSentence, type Failure, type FailureMessage } from "@imbue/workspace-ui/src/models/failure";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import type { ToastAction, ToastQueue } from "@imbue/workspace-ui/src/models/toasts";

/** What a failure's buttons reach through the shell: an agent, and the bug report. */
export type FailureHost = Pick<ShellConnection, "hasShell" | "askAgent" | "canReportBug" | "reportBug">;

/** A failure as Files shows it. */
export interface FilesFailure extends FailureMessage {
  /** The workspace's disk is full, so the toast offers to ask an agent to free up space. */
  readonly isOutOfSpace: boolean;
  /** The server says the item is not there any more, so a listing that still shows it is out of date. */
  readonly isGone: boolean;
  /** Nothing said why it failed: the sentence can only say to try again. */
  readonly isUnexplained: boolean;
}

/**
 * Where the operation that failed changes things, which decides how a refusal for want of access is said:
 * `into-folder` makes something in the folder on show (create, upload), `in-place` changes an item where it is (save,
 * delete, rename), `move` takes an item into another folder, and `open` only reads.
 */
export type FailedOperation = "into-folder" | "in-place" | "move" | "open";

/** What the person was doing when it failed. */
export interface Attempt {
  /** What failed, as the stem of the sentence ("Couldn't delete "notes.md""). */
  readonly whatFailed: string;
  readonly operation: FailedOperation;
  /** The same with every path in full ("delete /home/user/notes.md"), which the details open with: an agent asked
   *  about the failure reads what to do and where from them. Null where no agent is offered (an upload's row) or no
   *  path is involved. */
  readonly tried: string | null;
  /** The name of the item it was on, which a refusal that the item is gone names; null when it was on none of its
   *  own (a create, a listing). */
  readonly item: string | null;
}

/** The kinds of refusal the Files server sends (its `os_refusals.FailureKind`, which a test holds this list to). */
export const FILES_FAILURE_KINDS = [
  "not_found",
  "permission_denied",
  "read_only",
  "out_of_space",
  "destination_not_writable",
  "source_not_writable",
  "shared_read_only",
] as const;

export type FilesFailureKind = (typeof FILES_FAILURE_KINDS)[number];

/** A refusal's ``kind`` as one the Files server sends; null for none, or one this page does not know. */
export function filesFailureKindOf(kind: string | null): FilesFailureKind | null {
  return FILES_FAILURE_KINDS.find((known) => known === kind) ?? null;
}

/**
 * The causes whose words depend on what the person was doing, by kind and then operation: the server words each for
 * the request it was sent, which a page sees from where the person stands (a rename is a move to the server, and the
 * page has already named the folder a move goes into). Every other refusal is said in the server's sentence.
 */
const ACCESS_REFUSAL_BY_OPERATION: Readonly<
  Partial<Record<FilesFailureKind, Readonly<Partial<Record<FailedOperation, string>>>>>
> = {
  permission_denied: {
    "into-folder": "You don't have permission to change things in this folder.",
    "in-place": "You don't have permission to change things in the folder it's in.",
    move: "You don't have permission to move it.",
  },
  destination_not_writable: {
    "in-place": "You can't change things in the folder it's in.",
    move: "You can't add things to that folder.",
  },
};

const FREE_SPACE_LABEL = "Ask an agent to free up space";
const FREE_SPACE_INSTRUCTION =
  "Free up disk space in this workspace: remove what is safe to remove (caches, temporary files, build output), and " +
  "ask the user before deleting anything of theirs. Then tell the user what you removed and how much space it freed.";
/** What an agent asked about a refusal with a known cause is to do. */
const DO_IT_INSTRUCTION =
  "Do what the user was trying to do if you can; otherwise tell them why it can't be done and what they can do instead.";

/** What is said of an item an operation found gone. */
export function goneSentence(name: string): string {
  return `"${name}" isn't there anymore. It may have been moved or deleted.`;
}

/** Why the server refused: in Files' words where they depend on what the person was doing, else in the server's. */
function causeOf(failure: Failure & { cause: "user_facing" }, operation: FailedOperation): string {
  const kind = filesFailureKindOf(failure.kind);
  const byOperation = kind === null ? undefined : ACCESS_REFUSAL_BY_OPERATION[kind];
  return byOperation?.[operation] ?? failure.userFacingMessage;
}

function detailsOf(tried: string | null, details: string | null): string | null {
  const parts = [tried === null ? null : `Tried to ${tried}`, details].filter((part) => part !== null);
  return parts.length === 0 ? null : parts.join("\n");
}

/** A failed request in words. */
export function toldFailure(error: unknown, attempt: Attempt): FilesFailure {
  const failure = describeRequestFailure(error);
  const details = detailsOf(attempt.tried, failure.details);
  const kind = filesFailureKindOf(failure.kind);
  if (kind === "not_found" && attempt.item !== null) {
    return {
      message: goneSentence(attempt.item),
      details,
      isHelpOwed: false,
      isOutOfSpace: false,
      isGone: true,
      isUnexplained: false,
    };
  }
  if (failure.cause !== "user_facing") {
    const told = failureSentence(failure, attempt.whatFailed);
    return { ...told, details, isOutOfSpace: false, isGone: false, isUnexplained: told.isHelpOwed };
  }
  return {
    message: `${attempt.whatFailed}. ${causeOf(failure, attempt.operation)}`,
    details,
    isHelpOwed: !failure.isRoutine,
    isOutOfSpace: kind === "out_of_space",
    isGone: false,
    isUnexplained: false,
  };
}

/** A refusal Files words itself before asking the server (a name already taken, a move it cannot make), `tried`
 *  being what the person was doing, as an ``Attempt``'s. */
export function plainFailure(message: string, tried: string): FilesFailure {
  return {
    message,
    details: detailsOf(tried, null),
    isHelpOwed: true,
    isOutOfSpace: false,
    isGone: false,
    isUnexplained: false,
  };
}

/** One operation's failures, of one item or many, as one toast. */
export function toastFailures(
  toasts: ToastQueue,
  failures: readonly FilesFailure[],
  host: FailureHost,
  redraw: () => void,
): void {
  if (failures.length === 0) return;
  const message = [...new Set(failures.map((failure) => failure.message))].join("\n");
  const detailLines = failures.map((failure) => failure.details).filter((details) => details !== null);
  const details = detailLines.length === 0 ? null : detailLines.join("\n\n");
  if (host.hasShell() && failures.some((failure) => failure.isOutOfSpace)) {
    // Nothing else can be put right while the disk is full, so this ask stands in for the toasts' own.
    const request = { whatFailed: message, details, instruction: FREE_SPACE_INSTRUCTION };
    toasts.show(message, { isLasting: true, details, actions: [freeUpSpaceAction(toasts, host, redraw, request)] });
    return;
  }
  if (!failures.some((failure) => failure.isHelpOwed)) {
    toasts.show(message, { isLasting: false, details });
    return;
  }
  // A refusal with a known cause leaves nothing to diagnose, so the operation is the agent's task; a failure nothing
  // explained, alone or among the items of one change, stays a fault for it to find and fix.
  const agentRequest = failures.some((failure) => failure.isUnexplained)
    ? undefined
    : { whatFailed: message, details, instruction: DO_IT_INSTRUCTION };
  toasts.show(message, { isLasting: true, details, isHelpOwed: true, agentRequest });
}

/** What a toast's "Report a bug" does, decided as the toasts are drawn: null while the shell says Imbue Studio opens no
 *  bug report here. */
export function reportBugAction(host: FailureHost): ((failure: ReportedFailure) => void) | null {
  return host.canReportBug() ? (failure) => host.reportBug(failure) : null;
}

/**
 * The toast button that asks an agent to free up space: "Asking an agent…" until the shell says the chat opened, and
 * a note saying why when it could not, which offers the request as a draft and, in the agent's place, the bug report.
 * It goes to the main chat, which is already running: a chat made for the ask would need disk. An answer the shell
 * sends after a restart settled the ask as not answered replaces that note, unless the button was pressed again since.
 */
function freeUpSpaceAction(
  toasts: ToastQueue,
  host: FailureHost,
  redraw: () => void,
  request: AgentFixRequest,
): ToastAction {
  let isAsking = false;
  let presses = 0;
  let toldId: number | null = null;
  const tell = (press: number, outcome: StartOutcome): void => {
    if (press !== presses) return;
    if (toldId !== null) toasts.dismiss(toldId);
    toldId = null;
    if (!outcome.isOpened) {
      const { message, details, isHelpOwed } = outcome.failure;
      if (isHelpOwed) {
        // The shell tells a draft that could not be made itself.
        const draft = (): void => void askAgentToFix(host, request, { showsOutcome: true }, "draft");
        const actions = [{ label: DRAFT_ASK_LABEL, run: draft }];
        toasts.show(message, { isLasting: true, details, actions, isHelpOwed, isAgentWithheld: true });
      } else {
        toasts.show(message, { isLasting: false, details });
      }
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
    const options: StartOptions = { showsOutcome: true, onLateAnswer: (late) => tell(press, late) };
    const outcome = await askAgentToFix(host, request, options, "main-chat");
    if (press === presses) isAsking = false;
    tell(press, outcome);
  };
  return {
    label: FREE_SPACE_LABEL,
    run: () => void ask(),
    keepsToast: true,
    pending: { isPending: () => isAsking, label: ASKING_AN_AGENT_LABEL },
  };
}
