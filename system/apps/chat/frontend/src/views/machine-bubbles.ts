/**
 * Machine bubbles (the multiplayer spec, section 5.4): a stretch of the agent's output drawn as the agent speaking,
 * laid out the way another person's turn is -- the name above, the face beside the first piece -- so a turn reads
 * beside the people's bubbles rather than as a block of text.
 *
 * A stretch runs until a person speaks or another agent takes over. Its prose is a grey bubble per message; what it
 * did between them -- tool calls, steps, notices, the working dot -- sits on the chat on the bubbles' text edge, and
 * its cards line up with the bubbles. One line threads the stretch's pieces (see agent-rail).
 */

import m from "mithril";
import { MarkdownContent, type MarkdownReplies } from "../markdown";
import { getWorkspaceAgent } from "../models/Participants";
import { AgentRail, railNodeAttrs } from "./agent-rail";
import { FACE_COLUMN_CLASS, TURN_FACE_SIZE_PX, participantPicture } from "./participant-picture";

/** The bubbles' text edge, for a line of the agent's that stands outside a stretch (the status row under a person's
 *  turn, a notice not yet on the transcript): the face column plus a bubble's left padding. */
export const AGENT_TEXT_EDGE_CLASS = "ml-[calc(var(--width-face-gutter)+1rem)]";

/** The bubbles' text edge, for a piece inside a stretch's column. */
export const STRETCH_TEXT_EDGE_CLASS = "pl-4";

/** The pieces of a stretch, or of one part of it, 12px apart. */
export const STRETCH_PIECES_CLASS = "flex flex-col gap-3";

/** A prose bubble, as wide as its text and capped a little past a person's: 80% of the column plus its own padding. */
const AGENT_BUBBLE_CLASS =
  "machine-bubble w-fit min-w-0 max-w-[calc(80%+2rem)] rounded-[18px] bg-fill-active/75 px-4 py-2.5 " +
  "text-(length:--font-size-body) leading-normal text-primary wrap-break-word";

/** Where the agent's prose sits: a bubble of its own in a stretch, or plain text inside a piece that holds it (a
 *  step's unfurled work, a handoff's summary turn). */
export type ProseLayout = "bubble" | "plain";

export interface AgentProseAttrs {
  readonly text: string;
  readonly timestamp: string;
  readonly expansionKeyPrefix: string;
  readonly layout: ProseLayout;
  /** The stretch's first piece: its corner squares toward the face beside it. */
  readonly isStretchHead: boolean;
  /** How the prose's reply buttons send; absent where they cannot (they stay disabled). */
  readonly replies?: MarkdownReplies;
}

/** The agent's prose. Null for whitespace-only text (a lone newline before a tool call), which draws nothing. */
export function renderAgentProse(attrs: AgentProseAttrs): m.Vnode | null {
  const trimmed = attrs.text.trim();
  if (!trimmed) return null;
  const content = m(MarkdownContent, {
    content: trimmed,
    requestedAt: attrs.timestamp,
    expansionKeyPrefix: attrs.expansionKeyPrefix,
    replies: attrs.replies,
  });
  if (attrs.layout === "plain") return m("div", { class: "agent-prose", "data-agent-prose": "" }, content);
  return m(
    "div",
    {
      class: `agent-prose ${AGENT_BUBBLE_CLASS}${attrs.isStretchHead ? " rounded-tl-[4px]" : ""}`,
      "data-agent-prose": "",
      "data-machine-bubble": "",
      ...railNodeAttrs("box"),
    },
    content,
  );
}

/** A stretch of the agent's output: the agent's name over it, its face beside the first piece, and `parts` down
 *  the column on the line. Every part is keyed, as the line is. */
export function renderAgentTurn(parts: m.Children[]): m.Children {
  const agent = getWorkspaceAgent();
  const name = agent?.display_name ?? "Agent";
  return [
    m(
      "div",
      {
        class:
          `machine-bubbles-header mb-1 ${FACE_COLUMN_CLASS} ${STRETCH_TEXT_EDGE_CLASS} ` +
          "text-(length:--font-size-helper) leading-[1.4] text-faint",
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
        extra: "machine-face absolute top-0 left-0",
      }),
      m(
        "div",
        { class: `agent-stretch relative ${FACE_COLUMN_CLASS} ${STRETCH_PIECES_CLASS}`, "data-agent-stretch": "" },
        [m(AgentRail, { key: "agent-rail" }), ...parts],
      ),
    ]),
  ];
}
