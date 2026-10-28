/**
 * Machine bubbles (the multiplayer spec, section 5.4): the agent's prose drawn as the agent speaking, one bubble per
 * paragraph under the workspace agent's face and the workspace's name, so a turn reads beside the people's bubbles
 * rather than as a block of text. Tool chips, cards, and step timelines are not prose and stay outside.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import { getWorkspaceAgent } from "../models/Participants";
import { participantPicture } from "./participant-picture";

/** The width of a bubble's face, and the bubble group's header. */
export const MACHINE_FACE_SIZE_PX = 24;

/** Split markdown into paragraphs on blank lines, leaving fenced code blocks and ``<details>`` blocks whole
 *  whatever they contain: a toggle split from its body would open onto nothing. */
export function splitParagraphs(text: string): string[] {
  const paragraphs: string[] = [];
  let current: string[] = [];
  // The marker of the open fence (``` or ~~~); a fence closes only on its own marker.
  let openFence: string | null = null;
  // How many <details> blocks are open; blank lines inside one belong to it.
  let openDetails = 0;
  const flush = (): void => {
    const joined = current.join("\n").trim();
    if (joined) paragraphs.push(joined);
    current = [];
  };
  for (const line of text.split("\n")) {
    const fence = /^\s*(```|~~~)/.exec(line)?.[1] ?? null;
    if (fence !== null && openFence === null) openFence = fence;
    else if (fence === openFence) openFence = null;
    if (openFence === null) {
      openDetails += (line.match(/<details(\s|>)/gi) ?? []).length;
      openDetails = Math.max(0, openDetails - (line.match(/<\/details>/gi) ?? []).length);
    }
    if (openFence === null && openDetails === 0 && line.trim() === "") {
      flush();
      continue;
    }
    current.push(line);
  }
  flush();
  return paragraphs;
}

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

/** The agent's prose as bubbles: the first of a run squared at its top-left corner, where the header points.
 *  Null for text with no paragraph in it (a lone newline before a tool call), which draws nothing. */
export function machineBubbles(attrs: MachineBubblesAttrs): m.Vnode | null {
  const paragraphs = splitParagraphs(attrs.text);
  if (paragraphs.length === 0) return null;
  return m("div", { class: "machine-bubbles flex flex-col items-start gap-1.5", "data-machine-bubbles": "" }, [
    attrs.isHeaded ? bubbleHeader() : null,
    ...paragraphs.map((paragraph, index) =>
      m(
        "div",
        {
          class:
            "machine-bubble max-w-[80%] rounded-xl bg-fill-hover px-3.5 py-2 text-(length:--font-size-body) " +
            "leading-[1.4] text-primary wrap-break-word " +
            (attrs.isHeaded && index === 0 ? "rounded-tl-sm" : ""),
          "data-machine-bubble": "",
        },
        m(MarkdownContent, {
          content: paragraph,
          requestedAt: attrs.timestamp,
          expansionKeyPrefix: `${attrs.expansionKeyPrefix}:${index}`,
        }),
      ),
    ),
  ]);
}
