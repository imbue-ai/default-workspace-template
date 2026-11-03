/**
 * Machine bubbles (the multiplayer spec, section 5.4): the agent's prose drawn as the agent speaking, one bubble per
 * reply, laid out the way another person's turn is -- the name above, the face in a 24px gutter -- so a turn reads
 * beside the people's bubbles rather than as a block of text.
 *
 * The gutter belongs to the agent's column, not to a bubble: everything the agent produces (bubbles, tool chips,
 * cards, the step timeline, its status line) starts one face-and-gap in, and the name and face head only the first
 * row of each stretch of the agent's output, whatever that row holds. Tool chips, cards, and step timelines are not
 * prose and stay outside the bubbles, so a turn that interleaves prose and tool calls reads as several bubbles with
 * the tool work between them, all on one edge.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import { getWorkspaceAgent } from "../models/Participants";
import { participantPicture } from "./participant-picture";

/** The width of the agent's face: the same gutter another person's picture takes. */
export const MACHINE_FACE_SIZE_PX = 24;

/** The agent's column: the face's width plus the gap beside it, so the agent's work starts where its bubbles do.
 *  A margin, so a row in the message list's stretching flex column keeps the column's right edge. */
export const AGENT_COLUMN_CLASS = "ml-8";

export interface MachineBubblesAttrs {
  readonly text: string;
  readonly timestamp: string;
  readonly expansionKeyPrefix: string;
  /** Whether the bubble sits right beside the face, the first thing in its stretch: its top-left corner squares
   *  toward it, as another person's first bubble does. */
  readonly isBesideFace: boolean;
}

/** The agent's prose as one bubble, sized to its text. Null for whitespace-only text (a lone newline before a tool
 *  call), which draws nothing. */
export function machineBubbles(attrs: MachineBubblesAttrs): m.Vnode | null {
  const text = attrs.text.trim();
  if (!text) return null;
  const bubble = m(
    "div",
    {
      class:
        "machine-bubble max-w-[80%] rounded-[18px] bg-bubble-other px-4 py-2.5 text-(length:--font-size-body) " +
        "leading-[1.4] text-primary wrap-break-word " +
        (attrs.isBesideFace ? "rounded-tl-[4px]" : ""),
      "data-machine-bubble": "",
    },
    m(MarkdownContent, {
      content: text,
      requestedAt: attrs.timestamp,
      expansionKeyPrefix: attrs.expansionKeyPrefix,
    }),
  );
  return m("div", { class: "machine-bubbles flex w-full flex-col items-start", "data-machine-bubbles": "" }, bubble);
}

/** Lay `content` in the agent's column. A headed row also draws the agent's name over the column and its face in
 *  the gutter, level with the top of the content. */
export function agentColumn(isHeaded: boolean, content: m.Children): m.Children {
  if (!isHeaded) return m("div", { class: AGENT_COLUMN_CLASS }, content);
  const agent = getWorkspaceAgent();
  const name = agent?.display_name ?? "Agent";
  return [
    m(
      "div",
      {
        class: `machine-bubbles-header mb-1 ${AGENT_COLUMN_CLASS} text-(length:--font-size-helper) text-secondary`,
        "data-machine-header": "",
      },
      name,
    ),
    m("div", { class: "relative" }, [
      participantPicture({
        name,
        pictureUrl: agent?.profile_picture_url ?? null,
        color: agent?.color ?? null,
        size: MACHINE_FACE_SIZE_PX,
        extra: "machine-face absolute top-0.5 left-0",
      }),
      m("div", { class: AGENT_COLUMN_CLASS }, content),
    ]),
  ];
}
