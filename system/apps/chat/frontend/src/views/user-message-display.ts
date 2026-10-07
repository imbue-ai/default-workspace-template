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
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { MarkdownContent } from "../markdown";
import { parseMessageAttachments } from "../models/attachments";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { CompactionCause, UserMessageEvent } from "../models/Response";
import { AutocompactNotice } from "./AutocompactNotice";
import { classifyUserMessage, isHiddenUserMessage } from "./message-classification";
import { isBlockExpanded, setBlockExpanded, toggleBlockExpanded } from "./expansion-state";
import { UserMessageKind } from "./message-kinds";
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
  "message-user-bubble max-w-[80%] rounded-xl rounded-br-sm bg-user-bubble px-3.5 py-2 " +
  "text-(length:--font-size-body) leading-[1.4] text-primary wrap-break-word";

/** The collapsed, expandable "▸ <label>" chip used for every `SystemChip` kind
 *  (Stop hook / browser fleet / task-notification). Identical chrome regardless
 *  of source; only the label and body differ. Width-capped like the user
 *  bubbles on its rail (the assistant flow's blocks run full-width instead). */
function renderSystemChip(label: string, body: string, expansionKey: string): m.Vnode {
  return renderToolBlock({ headerText: label, inputText: body, extra: "max-w-[80%]", expansionKey });
}

/** What the "why?" popover beside a compaction pill says, for who started the compaction. */
export function compactionCauseText(cause: CompactionCause | null | undefined): string {
  switch (cause) {
    case "idle":
      return "Compacted while idle to keep replies fast and cheap. Change this under Auto-compact in the model menu.";
    case "manual":
      return "Compacted because you asked (/compact).";
    case "native":
      return "Your agent triggered compaction. You can ask it about its current setting, or tell it to change it.";
    default:
      return "Compacted to keep replies fast and cheap. Idle compaction is under Auto-compact in the model menu.";
  }
}

export function compactionWhyKey(eventId: string): string {
  return `compaction-why:${eventId}`;
}

/** The trailing "why?" button of a compaction pill and, while it is open, the popover it toggles.
 *  The open state lives in the expansion store, so it survives the row leaving the virtualized
 *  window and coming back. */
function renderCompactionWhy(event: UserMessageEvent): { button: m.Vnode; popover: m.Vnode | null } {
  const key = compactionWhyKey(event.event_id);
  const isOpen = isBlockExpanded(key);
  const popoverId = `${key}-popover`;
  return {
    button: m(
      Button,
      {
        variant: "ghost",
        sm: true,
        quiet: true,
        extra: "compaction-why-button",
        "aria-label": "Why was the context compacted?",
        "aria-expanded": isOpen ? "true" : "false",
        "aria-controls": isOpen ? popoverId : undefined,
        onclick: () => {
          toggleBlockExpanded(key);
        },
      },
      "why?",
    ),
    popover: isOpen
      ? m(
          "div",
          {
            id: popoverId,
            class:
              "compaction-why-popover mt-1.5 max-w-[360px] rounded-lg border border-default bg-surface px-3 py-2 " +
              "text-left text-(length:--font-size-helper) text-secondary shadow-md",
            role: "note",
          },
          compactionCauseText(event.compaction_cause),
        )
      : null,
  };
}

/**
 * Render a status message (e.g. "Context was compacted"), followed by its "why?" button.
 * When body text is present, renders an expandable toggle on the status pill
 * to show/hide the summary contents.
 */
function renderStatusMessage(label: string, body: string, expansionKey: string, event: UserMessageEvent): m.Vnode {
  const why = renderCompactionWhy(event);
  const statusLine = (pill: m.Vnode): m.Vnode =>
    m("div", { class: "message-system-status-line flex items-center gap-1" }, [pill, why.button]);
  if (!body) {
    return m("div", { class: "message-system-status-container" }, [
      statusLine(m("div", { class: "message-system-status" }, label)),
      why.popover,
    ]);
  }
  const expanded = isBlockExpanded(expansionKey);
  const toggleDetails = (pill: HTMLElement): void => {
    const container = pill.closest(".message-system-status-container");
    if (container) {
      setBlockExpanded(expansionKey, container.classList.toggle("message-system-status-container--expanded"));
    }
  };
  return m(
    "div",
    {
      class: `message-system-status-container${expanded ? " message-system-status-container--expanded" : ""}`,
    },
    [
      statusLine(
        m(
          "div",
          {
            class: "message-system-status message-system-status--toggleable",
            role: "button",
            tabindex: 0,
            onclick(e: Event) {
              toggleDetails(e.currentTarget as HTMLElement);
            },
            onkeydown(e: KeyboardEvent) {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                toggleDetails(e.currentTarget as HTMLElement);
              }
            },
          },
          [m("span", { class: "tool-call-chevron" }, "▸"), m("span", label)],
        ),
      ),
      why.popover,
      m("div", { class: "message-system-status-details" }, [m("div", { class: "message-system-status-body" }, body)]),
    ],
  );
}

/** A notice's lead when the backend's decision names none. */
export const NOTICE_FALLBACK_LABEL = "Background task completed";

/** The space below a notice's transcript row. */
export const NOTICE_ROW_SPACING_CLASS = "mb-2";

/** A one-line notice on the agent's rail: a tick, the lead, and the summary. The lead carries
 *  the weight because it is what the eye is scanning for down a long transcript; the summary is
 *  ordinary prose beside it. */
export function renderNotice(label: string, body: string): m.Vnode {
  return m("div", { class: "message-notice flex items-start gap-1.5 text-(length:--font-size-helper)" }, [
    m("span", { class: "mt-px shrink-0 text-accent" }, m.trust(icon("check", { size: 13, strokeWidth: 2.5 }))),
    m("span", { class: "min-w-0 text-secondary" }, [
      m("span", { class: "font-medium text-primary" }, `${label}:`),
      body ? ` ${body}` : null,
    ]),
  ]);
}

export function StableUserMessage(): m.Component<{ event: UserMessageEvent }> {
  let renderedEventId: string | null = null;
  // Whether the compaction pill's "why?" popover was open at the last render: toggling it is the
  // one change to an unchanged event that has to repaint.
  let renderedWhyOpen = false;
  return {
    onbeforeupdate(vnode) {
      const eventId = vnode.attrs.event.event_id;
      return eventId !== renderedEventId || isBlockExpanded(compactionWhyKey(eventId)) !== renderedWhyOpen;
    },
    view(vnode) {
      const event = vnode.attrs.event;
      renderedEventId = event.event_id;
      renderedWhyOpen = isBlockExpanded(compactionWhyKey(event.event_id));
      const cls = classifyUserMessage(event);
      // Every branch below draws the classification's body, never the raw content: for a
      // message the backend wrapped, the two differ and the bubble owes the user their words.
      //
      // The trailing "See attachment here: <markdown>" block is delivered to the
      // agent and kept visible in the bubble, where it renders as markdown so its
      // images show inline and other files as download links. The backend classifier
      // strips the block before its detectors run (harnesses/message_display.py),
      // so an appended attachment never changes the kind here either, and puts it back on the
      // body it hands a prompt, so splitting it off here still finds it.
      const { visibleText, attachmentBlock } = parseMessageAttachments(cls.body);

      if (cls.kind === UserMessageKind.SystemChip) {
        return renderSystemChip(cls.label ?? "System message", cls.body, `chip:${event.event_id}`);
      }
      if (cls.kind === UserMessageKind.Notice) {
        return renderNotice(cls.label ?? NOTICE_FALLBACK_LABEL, cls.body);
      }
      if (cls.kind === UserMessageKind.StatusMessage) {
        const label = cls.label ?? (cls.body || "Context was compacted");
        const body = cls.body && cls.body !== label ? cls.body : "";
        return renderStatusMessage(label, body, `status:${event.event_id}`, event);
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
 * Render a `user_message` as a top-level row, or `null` when it produces no row of its own
 * (hidden `/welcome`, or a skill expansion folded into its Skill tool block). A `SystemChip`
 * row gets the collapsed-system class; a genuine prompt gets the user-bubble class; a status
 * message gets the status-row class; a notice sits on the agent's rail instead.
 *
 * `isAutocompactNoticeAnchor` puts the one-time idle-compaction notice under a status row.
 */
export function renderUserMessage(event: UserMessageEvent, isAutocompactNoticeAnchor = false): m.Vnode | null {
  const kind = classifyUserMessage(event).kind;
  if (isHiddenUserMessage(event)) {
    return null;
  }
  const messageClass =
    kind === UserMessageKind.SystemChip
      ? "message message-system-collapsed mb-1 flex flex-col items-end"
      : kind === UserMessageKind.StatusMessage
        ? "message message-system-status-row"
        : kind === UserMessageKind.Notice
          ? `message message-notice-row ${NOTICE_ROW_SPACING_CLASS}`
          : `${USER_MESSAGE_ROW_CLASS} mb-5`;
  // id mirrors the assistant rows so the virtualized list can measure every
  // rendered row's height by querying ``.message-list > [id]``.
  return m("div", { id: event.event_id, class: messageClass, key: event.event_id }, [
    m(StableUserMessage, { event }),
    isAutocompactNoticeAnchor && kind === UserMessageKind.StatusMessage ? m(AutocompactNotice) : null,
  ]);
}
