/**
 * The handoff between two agents of one chat, as one timeline node: "Handing off to Codex…"
 * while it runs, "Handed off from Claude Code to Codex" once the switch has landed, expandable to
 * the retiring agent's summary turn and the prompt the successor started with. A landed switch
 * draws a rule under the node: everything below it is the successor's. One rendering whether
 * the node sits in a progress block's timeline or stands as a row of its own, and whether it
 * was built from the transcript
 * (``HandoffNode``) or, before the summary request has landed there, from the chat's snapshot
 * alone (``renderHandoffTailNode``).
 */

import m from "mithril";
import { statusDoneIcon, statusPendingIcon } from "@imbue/workspace-ui/src/components/icons";
import { getChatById } from "../models/Chats";
import type { ChatSnapshot, HandoffState } from "../models/Chats";
import type { ToolResultEvent } from "../models/Response";
import { harnessLabel } from "./harness-labels";
import { isBlockExpanded, toggleBlockExpanded } from "./expansion-state";
import { StableUserMessage, renderAssistantRun } from "./message-renderers";
import { isLiveHandoffRequest } from "./turn-grouping";
import type { HandoffNode } from "./turn-grouping";
import {
  TIMELINE_BODY_CLASS,
  TIMELINE_BULLET_CLASS,
  TIMELINE_CHEVRON_CLASS,
  TIMELINE_NODE_CLASS,
  timelineChevronStateClass,
  timelineSpinnerBullet,
  timelineTitleClass,
  type TimelineTone,
} from "./timeline-node";

type HandoffNodeStatus = "active" | "done" | "failed" | "cancelled";

/** What the node says and shows: read off the switch when it has landed, else off the chat's live switch. */
export function handoffNodeText(
  node: HandoffNode,
  chat: ChatSnapshot | undefined,
): { title: string; status: HandoffNodeStatus } {
  if (node.switch !== null) {
    return {
      title: `Handed off from ${harnessLabel(node.switch.from_harness)} to ${harnessLabel(node.switch.to_harness)}`,
      status: "done",
    };
  }
  const handoff = chat?.handoff ?? null;
  // A request from before the live switch was confirmed is an earlier switch's, called off: it must
  // not read as the live one while that runs.
  const isLive = node.request === null ? handoff !== null : isLiveHandoffRequest(node.request, handoff);
  if (!isLive || handoff === null) return { title: "Handoff called off", status: "cancelled" };
  return liveHandoffText(handoff, chat?.active_agent.harness ?? "");
}

/** The words for a switch still running, from the snapshot: a handoff names the harness it moves to, a
 *  rebind the account the same agent restarts on. */
function liveHandoffText(
  handoff: HandoffState,
  retiringHarness: string,
): { title: string; status: HandoffNodeStatus } {
  const from = harnessLabel(retiringHarness);
  const to = handoff.kind === "rebind" ? `${from} on ${handoff.target_label}` : harnessLabel(handoff.target_harness);
  if (handoff.phase === "failed") {
    return {
      title: handoff.kind === "rebind" ? `Could not restart ${to}` : `Could not hand off to ${to}`,
      status: "failed",
    };
  }
  return { title: handoff.kind === "rebind" ? `Restarting ${to}…` : `Handing off to ${to}…`, status: "active" };
}

function statusIcon(status: HandoffNodeStatus): m.Children {
  switch (status) {
    case "done":
      return m.trust(statusDoneIcon());
    case "active":
      return timelineSpinnerBullet();
    case "failed":
    case "cancelled":
      return m.trust(statusPendingIcon());
    default:
      return status satisfies never;
  }
}

/** A landed switch is finished work and greys out; one still running is the
 *  current thing; a failed or called-off one stays at full strength so the
 *  trouble does not fade into the timeline. */
function titleTone(status: HandoffNodeStatus): TimelineTone {
  if (status === "done") return "done";
  return status === "active" ? "current" : "upcoming";
}

export interface HandoffNodeOptions {
  /** True on the last node of a timeline, which caps the thread. */
  isLast: boolean;
  /** Where the node's expand state lives; a progress block scopes it to its section. */
  expansionKey: string;
}

/** The node: a status bullet, the title, and behind a chevron the retiring agent's summary turn and the
 *  successor's handoff prompt. */
export function renderHandoffNode(
  node: HandoffNode,
  chatId: string,
  toolResults: Map<string, ToolResultEvent>,
  options: HandoffNodeOptions,
): m.Vnode {
  const { title, status } = handoffNodeText(node, getChatById(chatId));
  const canExpand = node.request !== null || node.events.length > 0 || node.prompt !== null;
  const isExpanded = isBlockExpanded(options.expansionKey);
  const classes = [
    "pv-tl-node",
    "pv-tl-node--handoff",
    `pv-tl-node--handoff-${status}`,
    options.isLast ? "pv-tl-node--last pb-0" : "pb-[18px]",
    TIMELINE_NODE_CLASS,
  ].join(" ");
  return m("div", { class: classes, key: `handoff-${node.key}`, "data-handoff-status": status }, [
    m("div", { class: TIMELINE_BULLET_CLASS }, statusIcon(status)),
    m("div", { class: TIMELINE_BODY_CLASS }, [
      m(
        "button",
        {
          type: "button",
          class: timelineTitleClass(titleTone(status)),
          disabled: !canExpand,
          onclick: canExpand ? () => toggleBlockExpanded(options.expansionKey) : undefined,
        },
        [
          title,
          canExpand
            ? m(
                "span",
                { class: `${TIMELINE_CHEVRON_CLASS} ${timelineChevronStateClass(isExpanded)}` },
                m.trust("&rsaquo;"),
              )
            : null,
        ],
      ),
      isExpanded
        ? m(
            "div",
            // Flush under the title, no indent or left rule -- see the same
            // panel in ProgressBlock.
            { class: "pv-tl-expanded pv-expanded markdown-content mt-2.5 text-(length:--font-size-body)" },
            [
              node.request === null
                ? null
                : m(
                    "div",
                    { class: "message message-system-collapsed mb-1 flex flex-col items-end" },
                    m(StableUserMessage, { event: node.request }),
                  ),
              ...renderAssistantRun(node.events, toolResults, chatId),
              node.prompt === null
                ? null
                : m(
                    "div",
                    { class: "message message-system-collapsed mt-1 flex flex-col items-end" },
                    m(StableUserMessage, { event: node.prompt }),
                  ),
            ],
          )
        : null,
      // The boundary between the two agents' segments, once the switch has landed. Pulled left
      // under the bullet and painted opaque, so it spans the row and caps the thread.
      status === "done"
        ? m("div", {
            class: "pv-handoff-rule relative z-(--z-content) mt-3.5 -ml-[30px] h-2 border-t border-subtle bg-chat",
            "aria-hidden": "true",
          })
        : null,
    ]),
  ]);
}

/** The node for a switch the transcript does not show yet: the chat is converging (draining, or the summary
 *  request has not reached the stream) or restarting in place. Null once the transcript carries the request,
 *  or when nothing is converging. */
export function renderHandoffTailNode(chatId: string, hasOpenRequest: boolean): m.Vnode | null {
  const chat = getChatById(chatId);
  if (chat === undefined || chat.handoff === null || hasOpenRequest) return null;
  const node: HandoffNode = { key: `live-${chatId}`, request: null, events: [], switch: null, prompt: null };
  // Keyed like its siblings in the message list: a keyed list refuses an unkeyed member.
  return m(
    "div",
    {
      key: "handoff-tail",
      class: "handoff-tail-node progress-block mt-[18px] mb-[28px] text-(length:--font-size-body) leading-normal",
    },
    m("div.pv.pv--timeline.relative", [
      m("div.pv-timeline-nodes", renderHandoffNode(node, chatId, new Map(), { isLast: true, expansionKey: node.key })),
    ]),
  );
}
