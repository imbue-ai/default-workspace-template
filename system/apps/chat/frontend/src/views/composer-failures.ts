/**
 * What the composer's failure notice says, per operation. Pure, so each case can be held to its words without
 * mounting the composer.
 */

import { failureSentence } from "@imbue/workspace-ui/src/models/failure";
import type { Failure } from "@imbue/workspace-ui/src/models/failure";
import {
  describeRequestErrorKind,
  describeRequestFailure,
  isUnansweredRequest,
} from "@imbue/workspace-ui/src/models/request-error";
import type { SendFailureKind } from "@imbue/workspace-ui/src/models/request-error";
import { ChatNotStartedError } from "../models/Chats";
import { plainFailureLines } from "./failure-lines";
import type { FailureLines } from "./failure-lines";

const SEND_NOT_ANSWERED =
  "The workspace didn't answer, so Imbue Studio can't tell whether your message was delivered. Your text is back in " +
  "the box.";
const SEND_REFUSED = "Your message wasn't delivered. Your text is back in the box, so nothing was lost.";

/**
 * How the composer handles a failed send, which decides the notice's buttons: one of mngr's or the chat app's send
 * kinds; ``not_answered`` for a send nothing answered (it may have been delivered before the answer was lost);
 * ``unrecognised`` for a kind the server named that the composer does not know, which gets nothing kind-specific; and
 * ``chat_not_started`` for a send to a chat whose create failed, which has no agent behind it.
 */
export type ComposerFailureKind = SendFailureKind | "not_answered" | "unrecognised" | "chat_not_started";

/** Whether nothing answered the send: a gateway that gave up counts even when its body carries a reason. */
function isUnansweredSend(error: unknown, failure: Failure): boolean {
  return isUnansweredRequest(error) || failure.cause === "not_answered";
}

export function composerFailureKindOf(error: unknown): ComposerFailureKind {
  if (error instanceof ChatNotStartedError) return "chat_not_started";
  const failure = describeRequestFailure(error);
  if (isUnansweredSend(error, failure)) return "not_answered";
  const kind = describeRequestErrorKind(error);
  return kind === "unknown" && failure.kind !== null && failure.kind !== "unknown" ? "unrecognised" : kind;
}

/** A refused or unanswered send. The message is back in the composer whichever way it failed. The chat app's
 *  sentence for its case stands as written; this words the rest. */
export function describeSendFailure(error: unknown): FailureLines {
  const failure = describeRequestFailure(error);
  if (isUnansweredSend(error, failure)) {
    return { body: [SEND_NOT_ANSWERED], details: failure.details, isReportable: false };
  }
  const told = failureSentence(failure, "Your message wasn't delivered. Your text is back in the box");
  if (failure.cause === "user_facing") {
    return { body: [told.message], details: told.details, isReportable: told.isReportable };
  }
  return {
    body: [told.isReportable ? told.message : SEND_REFUSED],
    details: told.details,
    isReportable: told.isReportable,
  };
}

/** A first send whose launch of its chat was refused or got no answer: nothing was sent, and the text is back. */
export function describeLaunchFailure(error: unknown): FailureLines {
  const failure = describeRequestFailure(error);
  if (failure.cause === "not_answered") {
    return {
      body: [
        "The workspace didn't answer, so Imbue Studio can't tell whether this chat started. Your message is back in " +
          "the box.",
      ],
      details: failure.details,
      isReportable: false,
    };
  }
  if (failure.cause === "user_facing" && !failure.isReportable) {
    return {
      body: [failure.userFacingMessage, "Your message is back in the box."],
      details: failure.details,
      isReportable: false,
    };
  }
  if (failure.cause === "user_facing") {
    return {
      body: [
        "This chat couldn't be started. Your message is back in the box. Try again. If this persists, report a bug.",
      ],
      details: failure.details,
      isReportable: true,
    };
  }
  const told = failureSentence(failure, "This chat couldn't be started. Your message is back in the box");
  return { body: [told.message], details: told.details, isReportable: told.isReportable };
}

/** A switch of this chat to ``target`` that was refused or got no answer; ``isSendPath`` when a message rode it. */
export function describeSwitchFailure(error: unknown, target: string, isSendPath: boolean): FailureLines {
  const failure = describeRequestFailure(error);
  const backInTheBox = isSendPath ? ". Your message is back in the box" : "";
  if (failure.cause === "not_answered") {
    return {
      body: [`The workspace didn't answer, so Imbue Studio can't tell whether this chat moved to ${target}.`],
      details: failure.details,
      isReportable: false,
    };
  }
  const stillThere = `This chat couldn't be moved to ${target}. It's still running where it was${backInTheBox}`;
  if (failure.cause === "user_facing") {
    return {
      body: [`${stillThere}.`, failure.userFacingMessage],
      details: failure.details,
      isReportable: failure.isReportable,
    };
  }
  const told = failureSentence(failure, stillThere);
  return { body: [told.message], details: told.details, isReportable: told.isReportable };
}

/** A cancel of a running switch that was refused. A refusal says why in its own words (the switch is too far along,
 *  or it already finished); anything else is the fallback. */
export function describeCancelSwitchFailure(error: unknown): FailureLines {
  return plainFailureLines(error, "The switch couldn't be called off");
}

/** Whether a refused cancel only found the switch already over, which is not a failure. */
export function isNothingToCancel(error: unknown): boolean {
  return describeRequestFailure(error).kind === "nothing_to_cancel";
}

/** A Stop that was refused or got no answer. */
export function describeStopFailure(error: unknown): FailureLines {
  return plainFailureLines(error, "Couldn't stop the agent", {
    notAnswered: "The workspace didn't answer, so Imbue Studio can't tell whether the agent stopped.",
  });
}

/** The model and provider menu's Stop agent, refused or unanswered: unlike the composer's Stop it only stops, so a
 *  refusal leaves the agent running. */
export function describeMenuStopFailure(error: unknown): FailureLines {
  return plainFailureLines(error, "Couldn't stop the agent. It's still running", {
    notAnswered: "The workspace didn't answer, so Imbue Studio can't tell whether the agent stopped.",
  });
}

/** A shoulder tap that was refused or got no answer. */
export function describeTapFailure(error: unknown): FailureLines {
  return plainFailureLines(error, "Your queued messages weren't sent", {
    notAnswered: "The workspace didn't answer, so Imbue Studio can't tell whether your queued messages were sent.",
  });
}

/** A new chat started from inside this one (the switch dialog's or the failed-switch notice's "Start a new chat")
 *  that was refused or got no answer. */
export function describeNewChatFailure(error: unknown): FailureLines {
  return plainFailureLines(error, "The new chat couldn't be started", {
    notAnswered: "The workspace didn't answer, so Imbue Studio can't tell whether the new chat was started.",
  });
}
