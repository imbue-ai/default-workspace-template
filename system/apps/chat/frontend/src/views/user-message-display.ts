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
import { isBlockExpanded, toggleBlockExpanded } from "./expansion-state";
import { MarkdownContent } from "../markdown";
import { parseMessageAttachments } from "../models/attachments";
import {
  getOwner,
  getOwnParticipant,
  getParticipant,
  isOwnerKey,
  isViewerOwner,
  participantName,
} from "../models/Participants";
import type { UserMessageEvent } from "../models/Response";
import { participantPicture } from "./participant-picture";
import { classifyUserMessage, isHiddenUserMessage } from "./message-classification";
import type { UserMessageClass } from "./message-classification";
import { UserMessageKind } from "./message-kinds";
import { renderToolBlock } from "./ToolCallBlock";
import { appPartAttrs } from "@imbue/workspace-ui/src/themes/parts";

/** The user rail's shared recipes, owned here and composed by the queued and
 *  outgoing variants (QueuedMessageView / OutgoingMessageView). `message`,
 *  `message-user` and `message-user-bubble` are bare markers -- the Python e2e
 *  suite locates chat rows by them -- and the styling is the utilities beside
 *  them. The row recipe carries no bottom margin: each caller sets its own
 *  rhythm. */
export const USER_MESSAGE_ROW_CLASS = "message message-user flex flex-col items-end";
/** Another person's turn (the multiplayer spec, section 5.4): left-aligned, their picture beside the bubble. */
export const OTHER_PERSON_ROW_CLASS = "message message-user message-other flex flex-col items-start";
/** The side of the picture beside another person's bubble. */
export const AUTHOR_PICTURE_SIZE_PX = 24;

/** Who a user turn was said by, for drawing: null for the viewer's own turn (right-aligned, no name), else the
 *  author's key and name. A turn with no author is the owner's -- and so is one annotated under either of the
 *  owner's keys (the multiplayer spec, section 11): the synthetic ``owner`` from before the workspace was shared,
 *  or the account the roster marks as the owner, which an owner page opened before the share still calls
 *  ``owner`` -- so it is the viewer's own when the viewer is the owner, and the owner's entry otherwise. Until the
 *  page knows who it is, every turn reads as the viewer's own. */
export function authorOfTurn(
  event: Pick<UserMessageEvent, "author" | "author_name">,
): { key: string; name: string } | null {
  const own = getOwnParticipant();
  if (own === null) return null;
  if (event.author === undefined || isOwnerKey(event.author)) {
    if (isViewerOwner()) return null;
    const owner = getOwner();
    return owner === null ? null : { key: owner.key, name: owner.display_name };
  }
  if (event.author === own) return null;
  return { key: event.author, name: participantName(event.author, event.author_name ?? null) };
}

/** The bubble is the chat's declared `user-message` part (its app.toml `[theming]`), which themes may style. */
export const USER_MESSAGE_PART = appPartAttrs("chat", "user-message");

/** wrap-break-word: long unbreakable tokens (API keys, URLs) wrap inside the
 *  bubble instead of overflowing past its edge. Code inside a bubble is
 *  markdown-rendered content and takes .markdown-content's own rules. The
 *  geometry is the prototype's: an 18px round with a 4px corner squared toward
 *  the sender (the viewer's own at the bottom right, another's at the top left
 *  where their picture sits). */
const USER_BUBBLE_BASE =
  "message-user-bubble max-w-[80%] rounded-[18px] px-4 py-2.5 " +
  "text-(length:--font-size-body) leading-[1.4] text-primary wrap-break-word";
/** The viewer's own bubble: right-aligned, in the own-message tint. */
export const USER_BUBBLE_CLASS = `${USER_BUBBLE_BASE} rounded-br-[4px] bg-user-bubble`;
/** Another person's bubble: left-aligned under their name, in the not-yours grey. */
export const OTHER_BUBBLE_CLASS = `${USER_BUBBLE_BASE} rounded-tl-[4px] bg-bubble-other`;

/** The collapsed, expandable "▸ <label>" chip used for every `SystemChip` kind
 *  (Stop hook / browser fleet / task-notification). Identical chrome regardless
 *  of source; only the label and body differ. Width-capped like the user
 *  bubbles on its rail (the assistant flow's blocks run full-width instead). */
function renderSystemChip(label: string, body: string, expansionKey: string): m.Vnode {
  return renderToolBlock({ headerText: label, inputText: body, extra: "max-w-[80%]", expansionKey });
}

/** A notice's lead when the backend's decision names none. */
const NOTICE_FALLBACK_LABEL = "Background task completed";

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

/** A notice that is not on the transcript yet, as a row of its own. */
export function renderNoticeRow(key: string, rowClass: string, cls: UserMessageClass): m.Vnode {
  return m("div", { class: rowClass, key }, [renderNotice(cls.label ?? NOTICE_FALLBACK_LABEL, cls.body)]);
}

export function StableUserMessage(): m.Component<{ event: UserMessageEvent }> {
  let renderedEventId: string | null = null;
  // The page learns who it is beside the transcript (ensureParticipants), so a turn drawn before
  // then resolves no author; it is redrawn once the resolution changes.
  let renderedAuthorKey: string | null = null;
  return {
    onbeforeupdate(vnode) {
      return (
        vnode.attrs.event.event_id !== renderedEventId ||
        (authorOfTurn(vnode.attrs.event)?.key ?? null) !== renderedAuthorKey
      );
    },
    view(vnode) {
      const event = vnode.attrs.event;
      renderedEventId = event.event_id;
      const author = authorOfTurn(event);
      renderedAuthorKey = author?.key ?? null;
      const cls = classifyUserMessage(event);
      // Every branch below draws the classification's body, never the raw content: for a
      // message the backend wrapped, the two differ and the bubble owes the user their words.
      //
      // The trailing "See attachment here: <markdown>" block is delivered to the
      // agent and kept visible in the bubble, where it renders as markdown so its
      // images show inline and other files as links that open in Files. The backend classifier
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

      const bubbleChildren: m.Children[] = [];
      if (visibleText.length > 0) {
        bubbleChildren.push(m("div", { class: "message-content whitespace-pre-wrap" }, visibleText));
      }
      if (attachmentBlock !== null) {
        bubbleChildren.push(m(MarkdownContent, { content: attachmentBlock, requestedAt: event.timestamp }));
      }
      if (author === null) return m("div", { class: USER_BUBBLE_CLASS, ...USER_MESSAGE_PART }, bubbleChildren);
      // Another person's turn: their name above, their picture at the left, the bubble squared at its top-left.
      const entry = getParticipant(author.key);
      return m("div", { class: "message-other-turn flex w-full flex-col items-start", "data-author": author.key }, [
        m("div", { class: "message-author mb-1 ml-8 text-(length:--font-size-helper) text-secondary" }, author.name),
        m("div", { class: "flex w-full items-start gap-2" }, [
          participantPicture({
            name: author.name,
            pictureUrl: entry?.profile_picture_url ?? null,
            color: entry?.color ?? null,
            size: AUTHOR_PICTURE_SIZE_PX,
            extra: "message-author-picture mt-0.5",
          }),
          m("div", { class: OTHER_BUBBLE_CLASS, ...USER_MESSAGE_PART }, bubbleChildren),
        ]),
      ]);
    },
  };
}

/** What the "?" beside "Sent again" says. */
const RESENT_NOTE_HELP =
  "Imbue Studio couldn't confirm the agent received this message, so it sent it again. " +
  "If the first copy arrived too, the agent may see it twice.";

/** The line under a message the chat app sent a second time: "Sent again", and a "?" that opens a sentence on
 *  what that means. Its own expansion key, so the opened sentence survives the row being rebuilt. */
function renderResentNote(eventId: string): m.Vnode {
  const expansionKey = `resent-note:${eventId}`;
  const isExpanded = isBlockExpanded(expansionKey);
  return m("div", { class: "message-resent-note mt-1 flex max-w-[80%] flex-col items-end gap-1" }, [
    m("div", { class: "flex items-center gap-1 text-(length:--font-size-helper) text-faint" }, [
      m("span", "Sent again"),
      m(
        "button",
        {
          type: "button",
          class:
            "message-resent-note-help flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-full " +
            "border border-current text-[10px] leading-none hover:text-secondary",
          "aria-label": "What does sent again mean?",
          "aria-expanded": String(isExpanded),
          onclick: () => {
            toggleBlockExpanded(expansionKey);
          },
        },
        "?",
      ),
    ]),
    isExpanded
      ? m(
          "p",
          { class: "message-resent-note-text text-right text-(length:--font-size-helper) text-secondary" },
          RESENT_NOTE_HELP,
        )
      : null,
  ]);
}

/**
 * Render a `user_message` as a top-level row, or `null` when it produces no row of its own
 * (hidden `/welcome`, a skill expansion folded into its Skill tool block, or a compaction,
 * whose chips the transcript walk places in the agent's chip rows). A `SystemChip` row gets
 * the collapsed-system class; a genuine prompt gets the user-bubble class; a notice sits on
 * the agent's rail instead.
 */
export function renderUserMessage(event: UserMessageEvent, isResent = false): m.Vnode | null {
  const kind = classifyUserMessage(event).kind;
  if (isHiddenUserMessage(event) || kind === UserMessageKind.StatusMessage) {
    return null;
  }
  const messageClass =
    kind === UserMessageKind.SystemChip
      ? "message message-system-collapsed mb-1 flex flex-col items-end"
      : kind === UserMessageKind.Notice
        ? `message message-notice-row ${NOTICE_ROW_SPACING_CLASS}`
        : authorOfTurn(event) === null
          ? `${USER_MESSAGE_ROW_CLASS} mb-5`
          : `${OTHER_PERSON_ROW_CLASS} mb-5`;
  // id mirrors the assistant rows so the virtualized list can measure every
  // rendered row's height by querying ``.message-list > [id]``.
  return m("div", { id: event.event_id, class: messageClass, key: event.event_id }, [
    m(StableUserMessage, { event }),
    isResent ? renderResentNote(event.event_id) : null,
  ]);
}
