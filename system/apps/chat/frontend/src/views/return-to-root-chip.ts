/**
 * The agent going back to the workspace folder at the end of its turn, as one chip in the
 * transcript's chip rows.
 *
 * A Claude agent that ends its turn in another folder is sent back by a Stop hook, and answers
 * with a lone `cd`. The two are one housekeeping event rather than a turn of the conversation, so
 * the transcript walk folds them into this part (see `buildSections`): the hook's feedback, and the
 * `cd` once it lands.
 */

import m from "mithril";
import type { AssistantMessageEvent, ToolCall, UserMessageEvent } from "../models/Response";
import type { StatusChip } from "./ToolChipGroup";

export interface ReturnToRootPart {
  type: "return-to-root";
  /** The hook feedback's event id. */
  event_id: string;
  hook: UserMessageEvent;
  /** The agent's `cd` and the message that carried it, once it has landed. */
  cd: { event: AssistantMessageEvent; call: ToolCall } | null;
}

export function returnToRootPart(hook: UserMessageEvent): ReturnToRootPart {
  return { type: "return-to-root", event_id: hook.event_id, hook, cd: null };
}

/** The agent's answer to the hook when it is the lone `cd` the hook asks for, else null. */
export function cdAnswerOf(event: AssistantMessageEvent): ToolCall | null {
  if (event.text.trim() !== "" || event.tool_calls.length !== 1) return null;
  const call = event.tool_calls[0];
  return call.is_directory_change === true ? call : null;
}

const EXPLANATION =
  "Your agent finished in another folder, so it went back to the workspace folder, where its end-of-turn checks run.";

export function returnToRootChips(part: ReturnToRootPart): StatusChip[] {
  const command = part.cd?.call.action_target ?? "";
  return [
    {
      kind: "status",
      id: `return-to-root:${part.event_id}`,
      label: "Back to the workspace folder",
      icon: "folder",
      chipClass: "return-to-root-chip",
      detailClass: "return-to-root-detail",
      sections: [
        {
          kind: "content",
          render: () =>
            m("div", { class: "py-0.5 text-(length:--font-size-helper) leading-normal text-secondary" }, EXPLANATION),
        },
        ...(command ? [{ kind: "output" as const, marker: "return-to-root-command", text: command }] : []),
      ],
    },
  ];
}
