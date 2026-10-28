/**
 * Machine bubbles (the multiplayer spec, section 5.4): the agent's prose drawn as the agent speaking, one bubble per
 * reply under the workspace agent's face and the workspace's name, so a turn reads beside the people's bubbles
 * rather than as a block of text. Tool chips, cards, and step timelines are not prose and stay outside, so a turn
 * that interleaves prose and tool calls still reads as several bubbles with the tool work between them.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import { getWorkspaceAgent } from "../models/Participants";
import { participantPicture } from "./participant-picture";

/** The width of a bubble's face, and the bubble group's header. */
export const MACHINE_FACE_SIZE_PX = 24;

export interface MachineBubblesAttrs {
  readonly text: string;
  readonly timestamp: string;
  readonly expansionKeyPrefix: string;
  /** Whether this group opens the run: only the first group of a run wears the face and the name. */
  readonly isHeaded: boolean;
}

/** The face and the name above a run's first bubbles. */
function bubbleHeader(): m.Vnode {
  const agent = getWorkspaceAgent();
  const name = agent?.display_name ?? "Agent";
  return m("div", { class: "machine-bubbles-header mb-1 flex items-center gap-2", "data-machine-header": "" }, [
    participantPicture({ name, pictureUrl: agent?.profile_picture_url ?? null, size: MACHINE_FACE_SIZE_PX }),
    m("span", { class: "text-(length:--font-size-helper) font-medium text-secondary" }, name),
  ]);
}

/** The agent's prose as one bubble, squared at its top-left corner when it opens the run, where the header points.
 *  Null for whitespace-only text (a lone newline before a tool call), which draws nothing. */
export function machineBubbles(attrs: MachineBubblesAttrs): m.Vnode | null {
  const text = attrs.text.trim();
  if (!text) return null;
  return m("div", { class: "machine-bubbles flex flex-col items-start gap-1.5", "data-machine-bubbles": "" }, [
    attrs.isHeaded ? bubbleHeader() : null,
    m(
      "div",
      {
        class:
          "machine-bubble max-w-[80%] rounded-xl bg-fill-hover px-3.5 py-2 text-(length:--font-size-body) " +
          "leading-[1.4] text-primary wrap-break-word " +
          (attrs.isHeaded ? "rounded-tl-sm" : ""),
        "data-machine-bubble": "",
      },
      m(MarkdownContent, {
        content: text,
        requestedAt: attrs.timestamp,
        expansionKeyPrefix: attrs.expansionKeyPrefix,
      }),
    ),
  ]);
}
