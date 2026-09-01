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
import { isBlockExpanded, setBlockExpanded } from "./expansion-state";
import { KIND_SPEC, Rail, UserMessageKind } from "./message-kinds";

/** The collapsed, expandable "▸ <label>" chip used for every `SystemChip` kind
 *  (Stop hook / browser fleet / task-notification). Identical chrome regardless
 *  of source; only the label and body differ. */
function renderSystemChip(label: string, body: string, expansionKey: string): m.Vnode {
  return m("div", { class: `tool-call-block${isBlockExpanded(expansionKey) ? " tool-call-block--expanded" : ""}` }, [
    m(
      "div",
      {
        class: "tool-call-header",
        onclick(e: Event) {
          const block = (e.currentTarget as HTMLElement).parentElement;
          if (block) {
            setBlockExpanded(expansionKey, block.classList.toggle("tool-call-block--expanded"));
          }
        },
      },
      [m("span", { class: "tool-call-chevron" }, "▸"), m("span", label)],
    ),
    m("div", { class: "tool-call-details" }, [m("div", { class: "tool-call-input" }, [m("pre", m("code", body))])]),
  ]);
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
      if (cls.kind === UserMessageKind.StatusMessage) {
        return m("div", { class: "message-system-status" }, cls.body);
      }

      const bubbleChildren: m.Children[] = [];
      if (visibleText.length > 0) {
        bubbleChildren.push(m("div", { class: "message-content whitespace-pre-wrap" }, visibleText));
      }
      if (attachmentBlock !== null) {
        bubbleChildren.push(m(MarkdownContent, { content: attachmentBlock, requestedAt: event.timestamp }));
      }
      return m("div", { class: "message-user-bubble" }, bubbleChildren);
    },
  };
}

/**
 * Render a `user_message` as a top-level row, or `null` when it produces no
 * user-rail row (hidden `/welcome`, or a skill expansion folded into its Skill
 * tool block). A `SystemChip` row gets the collapsed-system class; a genuine
 * prompt gets the user-bubble class; a status message gets the status-row class.
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
      ? "message message-system-collapsed"
      : kind === UserMessageKind.StatusMessage
        ? "message message-system-status-row"
        : "message message-user";
  // id mirrors the assistant rows so the virtualized list can measure every
  // rendered row's height by querying ``.message-list > [id]``.
  return m("div", { id: event.event_id, class: messageClass, key: event.event_id }, [m(StableUserMessage, { event })]);
}
