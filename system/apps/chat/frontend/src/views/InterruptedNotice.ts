/**
 * The page of a chat whose agent stopped in the middle of its work (``active_agent.is_interrupted``): one fixed
 * line over the composer, with a Resume that sends the agent a message to carry on. Nothing resumes on its own,
 * since an agent that keeps hitting the same wall would only stop again; the line never guesses why it stopped.
 * A send of any kind clears it, so the notice is gone once Resume's message is on its way.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";
import { getChatById } from "../models/Chats";
import { addOutgoing, dropOutgoing, dropOutgoingNoTurnWillRetire } from "../models/OutgoingMessages";
import { mintMessageId, sendMessage } from "../models/Response";

export const INTERRUPTED_TITLE = "This chat stopped before it finished.";
export const RESUME_MESSAGE = "Please continue where you left off.";

export function InterruptedNotice(): m.Component<{ chatId: string }> {
  let isResuming = false;
  let resumeError: string | null = null;

  async function resume(chatId: string): Promise<void> {
    if (isResuming) return;
    isResuming = true;
    resumeError = null;
    const messageId = mintMessageId();
    const outgoingId = addOutgoing(chatId, RESUME_MESSAGE, messageId);
    try {
      const status = await sendMessage(chatId, RESUME_MESSAGE, messageId);
      dropOutgoingNoTurnWillRetire(chatId, outgoingId, status);
    } catch (error) {
      dropOutgoing(chatId, outgoingId);
      resumeError = describeRequestError(error);
    } finally {
      isResuming = false;
      m.redraw();
    }
  }

  return {
    view(vnode) {
      const { chatId } = vnode.attrs;
      const chat = getChatById(chatId);
      if (chat === undefined || chat.handoff !== null || !chat.active_agent.is_interrupted) {
        resumeError = null;
        return null;
      }
      return m(
        "div",
        {
          class:
            "interrupted-notice mx-auto mb-3 flex w-full max-w-(--width-message-column) flex-wrap items-center gap-2 " +
            "rounded-lg border border-danger-border bg-danger-surface p-3",
          role: "alert",
        },
        [
          m("p", { class: "interrupted-notice-title type-label flex-1 text-danger" }, INTERRUPTED_TITLE),
          m(
            Button,
            {
              variant: "primary",
              sm: true,
              extra: "interrupted-resume-button",
              readonly: isResuming,
              onclick: () => void resume(chatId),
            },
            "Resume",
          ),
          resumeError !== null
            ? m("p", { class: "interrupted-resume-error w-full text-sm text-danger" }, resumeError)
            : null,
        ],
      );
    },
  };
}
