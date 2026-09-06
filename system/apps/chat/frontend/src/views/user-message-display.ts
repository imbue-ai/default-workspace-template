/**
 * Rendering for a single `user_message` row, keyed by its `UserMessageKind`.
 *
 * This is the display half of the classify/display split: message-classification
 * decides WHAT a user_message is (its kind), this file decides how that kind
 * LOOKS. Both the top-level rows and the in-turn chips route through here, so a
 * given kind renders identically wherever it appears.
 *
 * See message-kinds.ts (`KIND_SPEC`) for the authoritative description of each
 * kind's rail and net visual; this file is the code that realises it.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import { parseMessageAttachments } from "../models/attachments";
import type { UserMessageEvent } from "../models/Response";
import { classifyUserMessage } from "./message-classification";
import { KIND_SPEC, Rail, UserMessageKind } from "./message-kinds";
import { renderToolBlock } from "./ToolCallBlock";

/** The user rail's shared recipes, owned here and composed by the queued and
 *  outgoing variants (QueuedMessageView / OutgoingMessageView). `message`,
 *  `message-user` and `message-user-bubble` are bare markers -- the Python e2e
 *  suite locates chat rows by them -- and the styling is the utilities beside
 *  them. The row recipe carries no bottom margin: each caller sets its own
 *  rhythm. */
export const USER_MESSAGE_ROW_CLASS = "message message-user flex flex-col items-end";

/** wrap-break-word: long unbreakable tokens (API keys, URLs) wrap inside the
 *  bubble instead of overflowing past its edge. Code inside a bubble is
 *  markdown-rendered content and takes .markdown-content's own rules. */
export const USER_BUBBLE_CLASS =
  "message-user-bubble max-w-[80%] rounded-xl rounded-br-sm bg-user-bubble px-[18px] py-3 " +
  "text-(length:--font-size-body) leading-normal text-primary wrap-break-word";

/** The collapsed, expandable "▸ <label>" chip used for every `SystemChip` kind
 *  (Stop hook / browser fleet / task-notification). Identical chrome regardless
 *  of source; only the label and body differ. Width-capped like the user
 *  bubbles on its rail (the assistant flow's blocks run full-width instead). */
function renderSystemChip(label: string, body: string, expansionKey: string): m.Vnode {
  return renderToolBlock({ headerText: label, inputText: body, extra: "max-w-[80%]", expansionKey });
}

export function StableUserMessage(): m.Component<{ event: UserMessageEvent }> {
  let renderedEventId: string | null = null;
  return {
    onbeforeupdate(vnode) {
      return vnode.attrs.event.event_id !== renderedEventId;
    },
    view(vnode) {
      const event = vnode.attrs.event;
      renderedEventId = event.event_id;
      const content = event.content || "";
      // The trailing "See attachment here: <markdown>" block is delivered to the
      // agent and kept visible in the bubble, where it renders as markdown so its
      // images show inline and other files as download links. The backend classifier
      // strips the block before its detectors run (harnesses/message_display.py),
      // so an appended attachment never changes the kind here either.
      const { visibleText, attachmentBlock } = parseMessageAttachments(content);
      const cls = classifyUserMessage(event);

      if (cls.kind === UserMessageKind.SystemChip) {
        return renderSystemChip(cls.label ?? "System message", cls.body, `chip:${event.event_id}`);
      }

      const bubbleChildren: m.Children[] = [];
      if (visibleText.length > 0) {
        bubbleChildren.push(m("div", { class: "message-content whitespace-pre-wrap" }, visibleText));
      }
      if (attachmentBlock !== null) {
        bubbleChildren.push(m(MarkdownContent, { content: attachmentBlock, requestedAt: event.timestamp }));
      }
      return m("div", { class: USER_BUBBLE_CLASS }, bubbleChildren);
    },
  };
}

/**
 * Render a `user_message` as a top-level row, or `null` when it produces no
 * user-rail row (hidden `/welcome`, or a skill expansion folded into its Skill
 * tool block). A `SystemChip` row gets the collapsed-system class; a genuine
 * prompt gets the user-bubble class.
 */
export function renderUserMessage(event: UserMessageEvent): m.Vnode | null {
  const kind = classifyUserMessage(event).kind;
  // A kind that does not render on the User rail (hidden /welcome + is_meta, or a
  // skill expansion relocated to the assistant rail) produces no row here.
  if (KIND_SPEC[kind].rail !== Rail.User) {
    return null;
  }
  const messageClass =
    kind === UserMessageKind.SystemChip
      ? "message message-system-collapsed mb-1 flex flex-col items-end"
      : `${USER_MESSAGE_ROW_CLASS} mb-5`;
  // id mirrors the assistant rows so the virtualized list can measure every
  // rendered row's height by querying ``.message-list > [id]``.
  return m("div", { id: event.event_id, class: messageClass, key: event.event_id }, [m(StableUserMessage, { event })]);
}
