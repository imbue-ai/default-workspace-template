/**
 * The buttons under a failed turn in the transcript, for the failures the user has a way out of that the turn's
 * words alone do not give them: another provider when this one's servers failed or the account ran into its limit,
 * and a new chat or a compaction when the conversation outgrew the model.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import { isSubagentPage } from "../document-meta";
import { getChatById } from "../models/Chats";
import type { AssistantMessageEvent } from "../models/Response";
import { mintMessageId, sendMessage } from "../models/Response";
import {
  DECLINED_COMPACT_TITLE,
  declinedCompactSentence,
  describeCompactFailure,
  isDeclinedCompact,
} from "./composer-failures";
import { raiseFailureNotice, startNewChatInPlaceOf } from "./MessageInput";
import { SWITCH_PROVIDER_LABEL, chooseProviderToSwitchChat } from "./provider-switch";

type FailedTurnRemedy = "switch_provider" | "shorten_conversation";

/** The way out a failed turn offers, if any. A sign-in failure has none here: its own note already offers the
 *  switch, beside signing in again. */
function remedyFor(event: AssistantMessageEvent): FailedTurnRemedy | null {
  if (event.is_auth_error) return null;
  if (event.api_error_kind === "context_too_long") return "shorten_conversation";
  if (event.is_provider_fault || event.api_error_kind === "rate_limit") return "switch_provider";
  return null;
}

/** Whether the buttons can act: they move, compact or replace the chat the page shows, so they need that chat (the
 *  chat list can land after the transcript) and are left out of a subagent view, whose turns are not the chat's. */
function isChatReadyForRemedies(chatId: string): boolean {
  return !isSubagentPage() && getChatById(chatId) !== undefined;
}

// The chats a Compact press's /compact is on its way to, so a second press does not send another.
const compactSendsInFlight = new Set<string>();

function sendCompact(chatId: string): Promise<unknown> {
  return sendMessage(chatId, "/compact", mintMessageId());
}

/** Send /compact for the user. A failure goes to the composer's notice: the conversation may be too long to compact
 *  as well, and the turn's other button stays where it is. */
async function compactConversation(chatId: string): Promise<void> {
  if (compactSendsInFlight.has(chatId)) return;
  compactSendsInFlight.add(chatId);
  m.redraw();
  try {
    await sendCompact(chatId);
  } catch (error) {
    if (isDeclinedCompact(error)) {
      raiseFailureNotice(chatId, {
        title: DECLINED_COMPACT_TITLE,
        describe: (declined) => ({ body: [declinedCompactSentence(declined)], details: null, isReportable: false }),
        error,
      });
    } else {
      raiseFailureNotice(chatId, {
        title: "Couldn't compact this conversation",
        describe: describeCompactFailure,
        error,
        // One nothing answered may be compacting already.
        retry:
          describeRequestFailure(error).cause === "not_answered"
            ? undefined
            : async () => {
                await sendCompact(chatId);
              },
      });
    }
  } finally {
    compactSendsInFlight.delete(chatId);
    m.redraw();
  }
}

/** What the buttons render from beyond the event itself, for a memoized turn to repaint on. */
export function failedTurnRemediesSignature(event: AssistantMessageEvent, chatId: string): string {
  const remedy = remedyFor(event);
  if (remedy === null) return "";
  if (!isChatReadyForRemedies(chatId)) return "unready";
  return remedy === "shorten_conversation" && compactSendsInFlight.has(chatId) ? "compacting" : "ready";
}

export function renderFailedTurnRemedies(event: AssistantMessageEvent, chatId: string): m.Children {
  const remedy = remedyFor(event);
  if (remedy === null || !isChatReadyForRemedies(chatId)) return null;
  const isCompactOnItsWay = compactSendsInFlight.has(chatId);
  return m(
    "div",
    { class: "message-api-error-remedies mt-[0.6em] flex flex-wrap gap-2" },
    remedy === "switch_provider"
      ? m(Button, { sm: true, onclick: () => chooseProviderToSwitchChat(chatId) }, SWITCH_PROVIDER_LABEL)
      : [
          m(Button, { sm: true, onclick: () => startNewChatInPlaceOf(chatId) }, "Start a new chat"),
          m(
            Button,
            { sm: true, disabled: isCompactOnItsWay, onclick: () => void compactConversation(chatId) },
            isCompactOnItsWay ? "Sending /compact…" : "Compact",
          ),
        ],
  );
}
