/**
 * The handoff between two agents of one chat, as one timeline node: "Handing off to Codex…"
 * while it runs, "Handed off from Claude to Codex" once the switch has landed, expandable to
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
import { harnessLabel } from "./agent-switch-chip";
import { isBlockExpanded, toggleBlockExpanded } from "./expansion-state";
import { StableUserMessage, renderAssistantMessageChildren } from "./message-renderers";
import type { HandoffNode } from "./turn-grouping";

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
  if (handoff === null) return { title: "Handoff called off", status: "cancelled" };
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
      return m(
        "span",
        { class: "pv-icon pv-icon--active inline-flex h-4 w-4 shrink-0 items-center justify-center text-accent" },
        m("span.spinner.spinner--sm.spinner--current"),
      );
    case "failed":
    case "cancelled":
      return m.trust(statusPendingIcon());
    default:
      return status satisfies never;
  }
}

const TITLE_CLASS =
  "pv-tl-title inline-flex cursor-pointer items-center appearance-none border-0 bg-transparent p-0 text-left " +
  "text-(length:--font-size-body) leading-[1.4] font-medium text-secondary disabled:cursor-default";

const CHEV_CLASS =
  "pv-chev ml-1.5 inline-block text-[18px] font-normal transition-transform duration-(--dur-base) ease-[ease]";

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
    "relative flex items-start gap-3.5",
  ].join(" ");
  return m("div", { class: classes, key: `handoff-${node.key}`, "data-handoff-status": status }, [
    m("div", { class: "pv-tl-bullet relative z-(--z-content) w-4 shrink-0 bg-chat py-px" }, statusIcon(status)),
    m("div", { class: "pv-tl-body min-w-0 flex-1" }, [
      m(
        "button",
        {
          type: "button",
          class: TITLE_CLASS,
          disabled: !canExpand,
          onclick: canExpand ? () => toggleBlockExpanded(options.expansionKey) : undefined,
        },
        [
          title,
          canExpand
            ? m(
                "span",
                { class: `${CHEV_CLASS} ${isExpanded ? "pv-chev--open rotate-90 text-primary" : "text-secondary"}` },
                m.trust("&rsaquo;"),
              )
            : null,
        ],
      ),
      isExpanded
        ? m(
            "div",
            {
              class:
                "pv-tl-expanded pv-expanded markdown-content mt-2.5 border-l-2 border-subtle py-1 pl-3.5 " +
                "text-(length:--font-size-body)",
            },
            [
              node.request === null
                ? null
                : m(
                    "div",
                    { class: "message message-system-collapsed mb-1 flex flex-col items-end" },
                    m(StableUserMessage, { event: node.request }),
                  ),
              ...node.events.flatMap((event) => renderAssistantMessageChildren(event, toolResults, chatId)),
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
