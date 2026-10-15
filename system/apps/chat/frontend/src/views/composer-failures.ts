/**
 * What the composer's failure notice says, per operation. Pure, so each case can be held to its words without
 * mounting the composer.
 */

import { failureSentence } from "@imbue/workspace-ui/src/models/failure";
import { describeRequestErrorKind, describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import type { SendFailureKind } from "@imbue/workspace-ui/src/models/request-error";
import { plainFailureLines } from "./failure-lines";
import type { FailureLines } from "./failure-lines";

export const SEND_NOT_ANSWERED =
  "The workspace didn't answer, so Imbue Studio can't tell whether your message was delivered. Your text is back in the box.";
export const SEND_TO_GONE_CHAT =
  "This chat is no longer here -- it was deleted. Your message is still in the box, so you can paste it into a new chat.";
const SEND_REFUSED = "Your message wasn't delivered. Your text is back in the box, so nothing was lost.";

/** What a send refused for a reason mngr classified means for the user, where the server said nothing of its own. */
function sendFailureKindLine(kind: SendFailureKind): string | null {
  switch (kind) {
    case "not_ready":
      return "The agent isn't ready for a new message yet. Wait a moment and try again.";
    case "agent_unreachable":
      return "Imbue Studio can't reach this agent. Restarting the agent will reset it and send your message.";
    default:
      return null;
  }
}

/** A refused or unanswered send. The message is back in the composer whichever way it failed. */
export function describeSendFailure(error: unknown): FailureLines {
  const failure = describeRequestFailure(error);
  const kind = describeRequestErrorKind(error);
  if (failure.cause === "not_answered") {
    return { body: [SEND_NOT_ANSWERED], details: failure.details, isReportable: false };
  }
  if (kind === "chat_gone") return { body: [SEND_TO_GONE_CHAT], details: failure.details, isReportable: false };
  const told = failureSentence(failure, "Your message wasn't delivered. Your text is back in the box");
  if (failure.cause === "user_facing") {
    return { body: [told.message], details: told.details, isReportable: told.isReportable };
  }
  return {
    body: [told.isReportable ? told.message : SEND_REFUSED, sendFailureKindLine(kind)],
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
