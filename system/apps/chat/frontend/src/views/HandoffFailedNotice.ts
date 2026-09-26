/**
 * The page of a chat whose switch failed (spec 5.10, 6): the reason the agent could not be started
 * or put on the model picked for it, over the composer, with a retry and a way to start a new chat
 * instead. A failed handoff retries on any signed-in account; a failed rebind retries on an account
 * of the same harness and lane, since its agent stays the chat's. A failed start leaves the chat
 * with no running agent meanwhile; either way the composer keeps working, since the chat app holds
 * what is typed for the retry.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";
import { getChatById } from "../models/Chats";
import type { ChatSnapshot, HandoffState } from "../models/Chats";
import { retryHandoff } from "../models/Handoffs";
import { getAccounts } from "../models/Providers";
import type { ProviderAccount } from "../models/Providers";
import { startChatOnAccount } from "../shell";
import { harnessLabel } from "./harness-labels";

/** The accounts a failed switch may be retried on: any for a handoff, the agent's own harness and lane for a rebind. */
export function retryableAccounts(chat: ChatSnapshot, handoff: HandoffState): ProviderAccount[] {
  const accounts = getAccounts();
  if (handoff.kind !== "rebind") return accounts;
  return accounts.filter(
    (account) => account.harness === chat.active_agent.harness && account.lane === handoff.target_lane,
  );
}

export function HandoffFailedNotice(): m.Component<{ chatId: string }> {
  // The account the retry runs on, once the user picks one; the failed target until then.
  let chosenAccountId: string | null = null;
  let isRetrying = false;
  let retryError: string | null = null;

  return {
    view(vnode) {
      const { chatId } = vnode.attrs;
      const chat = getChatById(chatId);
      const handoff = chat?.handoff ?? null;
      if (chat === undefined || handoff === null || handoff.phase !== "failed") {
        chosenAccountId = null;
        retryError = null;
        return null;
      }
      const accounts = retryableAccounts(chat, handoff);
      const failedTargetId = handoff.target_account_id;
      const selectedId = chosenAccountId ?? failedTargetId;
      // A handoff names the harness it moves to; a rebind's agent keeps its harness.
      const harness = harnessLabel(handoff.kind === "rebind" ? chat.active_agent.harness : handoff.target_harness);
      const title =
        handoff.failed_step === "model"
          ? `Could not set the model on ${harness}`
          : handoff.kind === "rebind"
            ? `Could not restart ${harness}`
            : `Could not start ${harness}`;

      async function retry(): Promise<void> {
        if (isRetrying) return;
        isRetrying = true;
        retryError = null;
        m.redraw();
        try {
          // Read at the press, not at the render: the picker may have changed since.
          await retryHandoff(chatId, chosenAccountId ?? failedTargetId);
        } catch (error) {
          retryError = describeRequestError(error);
        } finally {
          isRetrying = false;
          m.redraw();
        }
      }

      return m(
        "div",
        {
          class:
            "handoff-failed-notice mx-auto mb-3 flex w-full max-w-(--width-message-column) flex-col gap-2 " +
            "rounded-lg border border-danger-border bg-danger-surface p-3",
          role: "alert",
        },
        [
          m("p", { class: "handoff-failed-title type-label text-danger" }, title),
          m(
            "pre",
            {
              class:
                "handoff-failed-reason max-h-40 overflow-auto font-mono text-(length:--font-size-helper) " +
                "whitespace-pre-wrap text-primary",
            },
            handoff.error ??
              (handoff.failed_step === "model" ? "The model could not be set." : "The agent could not be started."),
          ),
          m("div", { class: "flex flex-wrap items-center gap-2" }, [
            m(
              "select",
              {
                class: inputClass({ extra: "handoff-retry-account" }),
                "aria-label": "Account to try",
                value: selectedId,
                onchange: (event: Event) => {
                  chosenAccountId = (event.target as HTMLSelectElement).value;
                },
              },
              accounts.map((account) =>
                m(
                  "option",
                  { value: account.id, key: account.id, selected: account.id === selectedId },
                  account.label,
                ),
              ),
            ),
            m(
              Button,
              {
                variant: "primary",
                sm: true,
                extra: "handoff-retry-button",
                readonly: isRetrying,
                onclick: () => void retry(),
              },
              isRetrying ? "Starting…" : "Try again",
            ),
            m(
              Button,
              {
                sm: true,
                extra: "handoff-new-chat-button",
                readonly: isRetrying,
                onclick: () => void startChatOnAccount(chosenAccountId ?? failedTargetId),
              },
              "Start a new chat instead",
            ),
          ]),
          retryError !== null ? m("p", { class: "handoff-retry-error text-sm text-danger" }, retryError) : null,
        ],
      );
    },
  };
}
