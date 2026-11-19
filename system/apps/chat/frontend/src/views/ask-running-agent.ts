/**
 * "Ask an agent to fix this" for a chat that cannot start. What stopped it may be the workspace itself (a full disk,
 * no memory left), which a new chat would meet too, so the task goes straight to a chat whose agent already exists,
 * through the chat app's own send route rather than through the shell.
 */

import { assistRequest } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection, StartOutcome } from "@imbue/workspace-ui/src/app_contract";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import { getChatById, getChats, getMainChatId } from "../models/Chats";
import type { ProvisionalChat } from "../models/Chats";
import { mintMessageId, sendMessage } from "../models/Response";
import { openChatPage } from "../shell";
import { getShellConnection } from "../shellConnection";
import { plainFailureLinesOf, plainFailureSentence } from "./failure-lines";
import type { FailureLines } from "./failure-lines";

/** The chat an ask about ``failingChatId`` goes to: the main chat when it is an agent chat, else the agent chat
 *  messaged most recently, the failing chat never being either; null when no other agent chat exists. */
export function chatToAskAbout(failingChatId: string): string | null {
  const others = getChats().filter((chat) => chat.kind === "agent" && chat.chat_id !== failingChatId);
  const mainChatId = getMainChatId();
  if (others.some((chat) => chat.chat_id === mainChatId)) return mainChatId;
  const byRecency = [...others].sort((one, other) => (other.last_messaged_at ?? 0) - (one.last_messaged_at ?? 0));
  return byRecency[0]?.chat_id ?? null;
}

/** The chat an ask about ``provisional``'s failed create goes to (``chatToAskAbout``); null as well for a chat that
 *  names no account, whose way out is a sign-in, which an agent cannot make. */
export function chatToAskAboutFailedCreate(provisional: ProvisionalChat): string | null {
  return provisional.account_id === "" ? null : chatToAskAbout(provisional.chat_id);
}

/** An ask of ``targetChatId`` that was refused or got no answer. It is told beside another chat's failure, where the
 *  send route's sentence alone ("The agent is waiting on something in its terminal.") would read as being about that
 *  chat, so it leads with the chat that was asked, and its details are headed with that chat to tell them from the
 *  failure's own. */
function describeAskFailure(error: unknown, targetChatId: string): FailureLines {
  const title = getChatById(targetChatId)?.title;
  const asked = title === undefined ? "the other chat" : `"${title}"`;
  const failure = describeRequestFailure(error, "change");
  const lead = `Couldn't ask ${asked}`;
  const lines: FailureLines =
    failure.cause === "user_facing"
      ? {
          body: [`${lead}.`, failure.userFacingMessage],
          details: failure.details,
          isReportable: failure.isReportable,
        }
      : plainFailureLinesOf(failure, lead, {
          notAnswered: `The workspace didn't answer, so Imbue Studio can't tell whether ${asked} was asked.`,
        });
  return { ...lines, details: lines.details === null ? null : `Asking ${asked}:\n${lines.details}` };
}

async function sendAsk(targetChatId: string, text: string): Promise<StartOutcome> {
  try {
    await sendMessage(targetChatId, text, mintMessageId());
  } catch (error) {
    const lines = describeAskFailure(error, targetChatId);
    return {
      isOpened: false,
      failure: { message: plainFailureSentence(lines), details: lines.details, isReportable: lines.isReportable },
    };
  }
  openChatPage(targetChatId);
  return { isOpened: true };
}

/** What ``askAgentToFix`` and ``AskAgentToFixButton`` take in place of the page's shell connection, so the ask is a
 *  message to ``targetChatId``, shown once it is sent. It is sent behind the assist command, as an ask through the
 *  shell is, so the agent runs the workspace's assist flow. A bug report about a refused ask still goes to the shell. */
export function askConnectionToChat(
  targetChatId: string,
): Pick<ShellConnection, "hasShell" | "askAgent" | "canReportBug" | "reportBug"> {
  return {
    // The send route carries the ask, so it can be made with or without a shell framing the page.
    hasShell: () => true,
    askAgent: (text) => sendAsk(targetChatId, assistRequest(text)),
    canReportBug: () => getShellConnection()?.canReportBug() ?? false,
    reportBug: (failure) => getShellConnection()?.reportBug(failure),
  };
}
