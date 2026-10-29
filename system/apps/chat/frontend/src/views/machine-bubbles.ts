/**
 * Machine bubbles (the multiplayer spec, section 5.4): the agent's prose drawn as the agent speaking, one bubble per
 * reply, laid out exactly as another person's turn is -- the name above, the face in a 24px gutter beside the
 * run's first bubble, every bubble starting where a person's does -- so a turn reads beside the people's bubbles
 * rather than as a block of text. Tool chips, cards, and step timelines are not prose and stay outside, so a turn
 * that interleaves prose and tool calls still reads as several bubbles with the tool work between them.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import { getWorkspaceAgent } from "../models/Participants";
import { participantPicture } from "./participant-picture";

/** The width of a bubble's face: the same gutter another person's picture takes. */
export const MACHINE_FACE_SIZE_PX = 24;

export interface MachineBubblesAttrs {
  readonly text: string;
  readonly timestamp: string;
  readonly expansionKeyPrefix: string;
  /** Whether this group opens the run: only the first group of a run wears the face and the name. */
  readonly isHeaded: boolean;
}

/** The agent's prose as one bubble in the person-turn geometry: the name over the run's first bubble, the face
 *  beside it, and a continuation group holding the same gutter with a spacer so every bubble lines up. Null for
 *  whitespace-only text (a lone newline before a tool call), which draws nothing. */
export function machineBubbles(attrs: MachineBubblesAttrs): m.Vnode | null {
  const text = attrs.text.trim();
  if (!text) return null;
  const agent = getWorkspaceAgent();
  const name = agent?.display_name ?? "Agent";
  const bubble = m(
    "div",
    {
      class:
        "machine-bubble max-w-[80%] rounded-[18px] bg-bubble-other px-4 py-2.5 text-(length:--font-size-body) " +
        "leading-[1.4] text-primary wrap-break-word " +
        (attrs.isHeaded ? "rounded-tl-[4px]" : ""),
      "data-machine-bubble": "",
    },
    m(MarkdownContent, {
      content: text,
      requestedAt: attrs.timestamp,
      expansionKeyPrefix: attrs.expansionKeyPrefix,
    }),
  );
  return m("div", { class: "machine-bubbles flex w-full flex-col items-start", "data-machine-bubbles": "" }, [
    attrs.isHeaded
      ? m(
          "div",
          {
            class: "machine-bubbles-header mb-1 ml-8 text-(length:--font-size-helper) text-secondary",
            "data-machine-header": "",
          },
          name,
        )
      : null,
    m("div", { class: "flex w-full items-start gap-2" }, [
      attrs.isHeaded
        ? participantPicture({
            name,
            pictureUrl: agent?.profile_picture_url ?? null,
            size: MACHINE_FACE_SIZE_PX,
            extra: "machine-face mt-0.5",
          })
        : m("span", { class: "w-6 shrink-0", "aria-hidden": "true" }),
      bubble,
    ]),
  ]);
}
