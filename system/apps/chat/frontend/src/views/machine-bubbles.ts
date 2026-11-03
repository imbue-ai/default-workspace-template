/**
 * Machine bubbles (the multiplayer spec, section 5.4): a stretch of the agent's output drawn as the agent speaking,
 * laid out the way another person's turn is -- the name above, the face in the gutter -- so a turn reads beside the
 * people's bubbles rather than as a block of text.
 *
 * A stretch runs until a person speaks or another agent takes over, and all of it sits in one grey bubble: the
 * agent's prose, its tool calls, the step timeline, its cards, and the system notices between them, every one
 * starting on the bubble's text edge. The bubble is as wide as the widest thing in it, up to a person's bubble's cap.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import { getWorkspaceAgent } from "../models/Participants";
import { FACE_COLUMN_CLASS, TURN_FACE_SIZE_PX, participantPicture } from "./participant-picture";

/** The agent bubble's text edge, for a line of the agent's that sits below the bubble rather than in it: the face
 *  column plus the bubble's own left padding. */
export const AGENT_TEXT_EDGE_CLASS = "ml-[calc(var(--width-face-gutter)+1rem)]";

/** A stretch's bubble: sized to its widest content and capped at 80% of the whole row, as another person's bubble is
 *  (its column starts a face's width in). The first and last children give up their outer margins so the padding
 *  alone frames the content. */
const AGENT_BUBBLE_CLASS =
  "machine-bubble w-fit min-w-0 max-w-[calc((100%+var(--width-face-gutter))*0.8)] rounded-[18px] rounded-tl-[4px] " +
  "bg-agent-bubble px-4 py-2.5 text-(length:--font-size-body) leading-[1.4] text-primary wrap-break-word " +
  "[&>*:first-child]:mt-0 [&>*:last-child]:mb-0";

/** The agent's prose. Null for whitespace-only text (a lone newline before a tool call), which draws nothing. */
export function renderAgentProse(text: string, timestamp: string, expansionKeyPrefix: string): m.Vnode | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  return m(
    "div",
    { class: "agent-prose", "data-agent-prose": "" },
    m(MarkdownContent, { content: trimmed, requestedAt: timestamp, expansionKeyPrefix }),
  );
}

/** A stretch of the agent's output: the agent's name over its bubble, its face in the gutter beside the bubble's
 *  top, and `content` in the bubble. */
export function renderAgentTurn(content: m.Children): m.Children {
  const agent = getWorkspaceAgent();
  const name = agent?.display_name ?? "Agent";
  return [
    m(
      "div",
      {
        class: `machine-bubbles-header mb-1 ${FACE_COLUMN_CLASS} text-(length:--font-size-helper) text-secondary`,
        "data-machine-header": "",
      },
      name,
    ),
    m("div", { class: "relative" }, [
      participantPicture({
        name,
        pictureUrl: agent?.profile_picture_url ?? null,
        color: agent?.color ?? null,
        size: TURN_FACE_SIZE_PX,
        extra: "machine-face absolute top-0.5 left-0",
      }),
      m("div", { class: `${FACE_COLUMN_CLASS} flex flex-col items-start` }, [
        m("div", { class: AGENT_BUBBLE_CLASS, "data-machine-bubble": "" }, content),
      ]),
    ]),
  ];
}
