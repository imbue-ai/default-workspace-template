/**
 * The line under a user's message that shows the step the chat app made for it (the snapshot's
 * ``active_agent.message_steps``): an icon for where it stands, its title, and a faint word for its
 * state -- to do, in progress, or done with the agent's summary.
 *
 * A transcript turn carries no message id, so a bubble finds its step by its text: the newest step
 * made for a message with the same words. An outgoing bubble knows its send-time id and finds its
 * step by that first.
 */

import m from "mithril";
import { statusDoneIcon, statusPendingIcon, statusRingIcon } from "@imbue/workspace-ui/src/components/icons";
import { getMessageStepsForChat } from "../models/Chats";
import type { MessageStep } from "../models/Chats";

/** A message's text as two copies of it compare (the agent hook's ``normalized_message_text``). */
export function normalizedMessageText(text: string): string {
  return text.replace(/\r\n/g, "\n").trim();
}

/** The step made for a message, by its send-time id when one is known, else the newest with its words. */
export function findMessageStep(steps: readonly MessageStep[], text: string, messageId?: string): MessageStep | null {
  if (messageId !== undefined) {
    const byId = steps.find((step) => step.message_id === messageId);
    if (byId !== undefined) return byId;
  }
  const wanted = normalizedMessageText(text);
  if (wanted === "") return null;
  for (let index = steps.length - 1; index >= 0; index--) {
    if (normalizedMessageText(steps[index].message_text) === wanted) return steps[index];
  }
  return null;
}

/** The faint word for where a step stands. */
export function messageStepStateLabel(step: MessageStep): string {
  if (step.status === "closed") return "Done";
  return step.status === "in_progress" ? "In progress" : "To do";
}

function stateIcon(step: MessageStep): m.Children {
  if (step.status === "closed") return m.trust(statusDoneIcon());
  if (step.status === "in_progress") return m.trust(statusRingIcon());
  return m.trust(statusPendingIcon());
}

/** The step line for one message, or null when no step was made for it. */
export function renderMessageStepLine(chatId: string, text: string, messageId?: string): m.Vnode | null {
  const step = findMessageStep(getMessageStepsForChat(chatId), text, messageId);
  if (step === null) return null;
  const isDone = step.status === "closed";
  return m(
    "div",
    {
      class: "message-step-line mt-1 flex max-w-[80%] items-start gap-1.5 text-(length:--font-size-helper)",
      "data-step-id": step.step_id,
      "data-step-state": messageStepStateLabel(step).toLowerCase().replace(" ", "-"),
    },
    [
      m("span", { class: "message-step-line__icon mt-px flex-none" }, stateIcon(step)),
      m("span", { class: "flex min-w-0 flex-col" }, [
        m("span", { class: "flex min-w-0 items-baseline gap-1.5" }, [
          m(
            "span",
            { class: `message-step-line__title truncate ${isDone ? "text-faint" : "text-secondary"}` },
            step.title,
          ),
          m("span", { class: "message-step-line__state flex-none text-faint" }, messageStepStateLabel(step)),
        ]),
        isDone && step.summary !== ""
          ? m("span", { class: "message-step-line__summary text-faint" }, step.summary)
          : null,
      ]),
    ],
  );
}
