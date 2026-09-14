/**
 * The page of a chat whose switch failed (spec 5.10): the reason the new agent could not be
 * started, over the composer, with a retry on any signed-in account. The chat has no running
 * agent meanwhile; the composer keeps working, since the chat app holds what is typed for the
 * retry.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";
import { getChatById } from "../models/Chats";
import { retryHandoff } from "../models/Handoffs";
import { getAccounts } from "../models/Providers";
import { harnessLabel } from "./agent-switch-chip";

export function HandoffFailedNotice(): m.Component<{ chatId: string }> {
  // The account the retry runs on, once the user picks one; the failed target until then.
  let chosenAccountId: string | null = null;
  let isRetrying = false;
  let retryError: string | null = null;

  return {
    view(vnode) {
      const { chatId } = vnode.attrs;
      const handoff = getChatById(chatId)?.handoff ?? null;
      if (handoff === null || handoff.phase !== "failed") {
        chosenAccountId = null;
        retryError = null;
        return null;
      }
      const accounts = getAccounts();
      const failedTargetId = handoff.target_account_id;
      const selectedId = chosenAccountId ?? failedTargetId;

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
          m(
            "p",
            { class: "handoff-failed-title type-label text-danger" },
            `Could not start ${harnessLabel(handoff.target_harness)}`,
          ),
          m(
            "pre",
            {
              class:
                "handoff-failed-reason max-h-40 overflow-auto font-mono text-(length:--font-size-helper) " +
                "whitespace-pre-wrap text-primary",
            },
            handoff.error ?? "The new agent could not be started.",
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
          ]),
          retryError !== null ? m("p", { class: "handoff-retry-error text-sm text-danger" }, retryError) : null,
        ],
      );
    },
  };
}
