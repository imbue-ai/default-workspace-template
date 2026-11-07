/**
 * Renders the optimistic outgoing bubbles (see models/OutgoingMessages) at the
 * very tail of the transcript -- below the committed turns AND below the queued
 * group, so a just-sent message shows immediately as the last thing(s) until the
 * harness-sourced state catches up. Drawn exactly like the delivered turn that
 * replaces it, so a send reads as sent at once (contract A2). Where the send is (on
 * its way, or waiting for the agent to come up) is the status row under it
 * (ActivityIndicator), not the bubble. A failed send is NOT rendered here -- the composer handles that (popup +
 * text restored), and the bubble is dropped.
 */
import m from "mithril";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import type { DisplayDecision, MessageAuthor } from "../models/Chats";
import type { OutgoingMessage } from "../models/OutgoingMessages";
import { classifyDecidedMessage } from "./message-classification";
import { UserMessageKind } from "./message-kinds";
import {
  NOTICE_ROW_SPACING_CLASS,
  authorOfTurn,
  isSameSpeaker,
  renderNoticeRow,
  renderUserBubble,
  userMessageRowClass,
} from "./user-message-display";
import type { TurnAuthor } from "./user-message-display";

// Bare markers for tests, beside the user rail's own row recipe.
const OUTGOING_ROW_MARKERS = "outgoing-message outgoing-message--sending";

// The committed user row's spacing, so consecutive not-yet-delivered bubbles stand apart and the
// real turn replaces one without a reflow.
const STANDALONE_ROW_SPACING_CLASS = "mb-5";

export interface NotYetRealBubble {
  key: string;
  /** The message's text; the bubble shows the body ``displayDecision`` gives it, and the text itself without one. */
  content: string;
  /** True inside a group whose own gap spaces its bubbles (the queued group), so the row
   *  carries no bottom margin of its own. */
  isGroupSpaced?: boolean;
  /** Marker classes added to the row, for a caller whose bubbles a test or a style picks out. */
  extraRowClass?: string;
  /** The backend's render decision for the text; a notice (a background-task report) draws as one. */
  displayDecision?: DisplayDecision;
  /** Who sent it, for a message the backend reports (queued, or held); absent for the viewer's own send, which is
   *  drawn as theirs. */
  author?: MessageAuthor;
}

/** The run of one person's bubbles as pending sends are drawn down the page: open while the last thing drawn is a
 *  bubble of theirs, so the next one by the same person carries on it and wears no name ("You" for the viewer's own),
 *  as the turn it becomes will not. Each send drawn moves it on: a bubble opens it on its author, a notice (the
 *  agent's line, not a person's words) closes it. */
export interface PendingBubbleRun {
  isOpen: boolean;
  /** Whose bubbles the run holds while it is open: null for the viewer's own. */
  author: TurnAuthor | null;
}

/** A run nothing has opened yet. */
export function closedPendingRun(): PendingBubbleRun {
  return { isOpen: false, author: null };
}

/** Whether a bubble by `author` carries on `run`, which it then holds. */
export function continuesPendingRun(run: PendingBubbleRun, author: TurnAuthor | null): boolean {
  const isContinued = run.isOpen && isSameSpeaker(run.author, author);
  run.isOpen = true;
  run.author = author;
  return isContinued;
}

/** A user message that is not yet a real turn, drawn as the user bubble it will become, or as the
 *  one-line notice when the backend decided its text renders as one (a background-task report).
 *  Every not-yet-delivered message renders through here (optimistic, held by a switch, re-sent by
 *  a tap), so they look the same and the handoff to the real turn is invisible. */
export function renderNotYetRealBubble(bubble: NotYetRealBubble, run: PendingBubbleRun = closedPendingRun()): m.Vnode {
  const cls = classifyDecidedMessage(bubble.content, bubble.displayDecision);
  const isNotice = cls.kind === UserMessageKind.Notice;
  const author = bubble.author === undefined ? null : authorOfTurn(bubble.author);
  const [baseClass, spacingClass] = isNotice
    ? ["message-notice-row", NOTICE_ROW_SPACING_CLASS]
    : [`${userMessageRowClass(author)} ${OUTGOING_ROW_MARKERS}`, STANDALONE_ROW_SPACING_CLASS];
  const rowClass = [baseClass, bubble.isGroupSpaced === true ? null : spacingClass, bubble.extraRowClass ?? null]
    .filter((part) => part !== null)
    .join(" ");
  if (isNotice) {
    run.isOpen = false;
    return renderNoticeRow(bubble.key, rowClass, cls);
  }
  const isContinued = continuesPendingRun(run, author);
  return m(
    "div",
    { class: rowClass, key: bubble.key },
    renderUserBubble(author, [m("div", { class: "message-content whitespace-pre-wrap" }, cls.body)], { isContinued }),
  );
}

/** The optimistic outgoing bubbles for a chat, in send order, drawn down `run`. Returns [] when
 *  there are none. */
export function renderOutgoingMessages(chatId: string, run: PendingBubbleRun = closedPendingRun()): m.Vnode[] {
  return getOutgoingMessages(chatId).map((outgoing: OutgoingMessage) =>
    renderNotYetRealBubble(
      {
        key: outgoing.id,
        content: outgoing.content,
        displayDecision: outgoing.displayDecision,
        author: outgoing.author,
      },
      run,
    ),
  );
}
