/**
 * Progress block: timeline rendering of one section (one user turn), as a part of
 * a stretch of the agent's output.
 *
 * The section is a flat, ordered list of timeline items produced by the
 * transcript walk (see turn-grouping): step nodes, ungrouped work/prose runs
 * (including a step's ejected closing prose), and chips. Step nodes carry their
 * own grouped events; expanding a step reveals that grouped work. The wrap-up
 * reply renders below the timeline. Every piece stands on the stretch's line
 * (see agent-rail), which runs through the step bullets as it does through the
 * chip icons between them.
 *
 * This component renders structure it is given; it does no grouping or
 * ordering itself.
 */

import m from "mithril";
import { renderMarkdown } from "../markdown";
import { isBlockExpanded, toggleBlockExpanded } from "./expansion-state";
import type { ToolResultEvent, AssistantMessageEvent } from "../models/Response";
import { renderAssistantRun, renderPermissionItem, renderUserMessage, stretchRun } from "./message-renderers";
import { STRETCH_PIECES_CLASS } from "./machine-bubbles";
import type { StepNode, StepStatus, TimelineItem } from "./turn-grouping";
import { renderHandoffNode } from "./handoff-node";
import {
  TIMELINE_BODY_CLASS,
  TIMELINE_CHEVRON_CLASS,
  TIMELINE_NODE_CLASS,
  TIMELINE_STEPS_CLASS,
  renderTimelineBullet,
  renderTimelineTitleText,
  timelineChevronStateClass,
  timelineSpinnerBullet,
  timelineTitleClass,
  type TimelineTone,
} from "./timeline-node";
import { statusDoneIcon, statusPendingIcon, statusRingIcon } from "@imbue/workspace-ui/src/components/icons";

interface ProgressBlockAttrs {
  /** Timeline items in transcript order (steps, ungrouped runs, chips). */
  items: TimelineItem[];
  /** The wrap-up reply, rendered below the timeline. */
  trailing_reply: AssistantMessageEvent[];
  /** Prebuilt tool_call_id -> tool_result map for the whole stream (skill
   *  expansions already folded in). Lookups by id work even though a section
   *  only references a subset. */
  toolResults: Map<string, ToolResultEvent>;
  chatId: string;
  /** Optional DOM id for the root, also the prefix that keeps the expansion
   *  state of the steps and handoff nodes inside this block apart from another block's. */
  id?: string;
  /** The block is its stretch's first part: a bubble that opens it squares its corner toward the face. */
  isStretchHead?: boolean;
  /** The step the agent's next words join (SectionView.live_step_id), and the reply it is still
   *  writing, read at draw time: the draft shows in that step as its latest words would. */
  liveStepId?: string | null;
  replyDraft?: () => AssistantMessageEvent | null;
}

/* Styling.
 * Utilities in the markup; the pv-* class names stay as bare markers. In
 * style.css for this view: the narration shimmer (a keyframe +
 * background-clip machine), the narration/expanded markdown-child rules
 * (rendered content), and the expanded panel's tool-block override (a
 * contextual rule over shared markup). */

/** A step's status as the title reads it: only a closed step greys out, and only
 *  the live frontier one shimmers (a settled active step -- a carryover, an idle
 *  agent -- reads like any other open step). */
function titleTone(step: StepNode): TimelineTone {
  if (step.status === "done") return "done";
  return step.status === "active" && step.is_frontier ? "current" : "upcoming";
}

function statusIcon(status: StepStatus, is_frontier: boolean): m.Children {
  if (status === "done") {
    return m.trust(statusDoneIcon());
  }
  if (status === "active") {
    // The live frontier step spins; any other active step is settled (a
    // static partial ring) -- a past-turn carryover, an idle agent, or a step
    // superseded by a later one.
    if (!is_frontier) {
      return m.trust(statusRingIcon());
    }
    return timelineSpinnerBullet();
  }
  return m.trust(statusPendingIcon());
}

/** Sub-caption under the step title:
 *   - done + summary -> the close summary
 *   - active + narration (and not expanded) -> latest in-step narration, or the reply the agent
 *     is still writing in the live step
 *   - otherwise nothing. */
function renderStepCaption(step: StepNode, isExpanded: boolean, draft: AssistantMessageEvent | null): m.Vnode | null {
  if (step.status === "done") {
    return step.summary
      ? m(
          "div",
          // Faint, matching the greyed-out title of the closed step it belongs to.
          { class: "pv-tl-summary mt-[3px] pl-0.5 text-(length:--font-size-helper) leading-normal text-faint" },
          step.summary,
        )
      : null;
  }
  if (isExpanded) return null;
  const narration = draft?.text || step.narration;
  if (!narration) return null;
  // The narration look (muted italic; the frontier one's shimmer) stays in
  // style.css -- it styles markdown-rendered children and the shimmer is a
  // keyframe machine.
  const captionClass = step.is_frontier ? "pv-tl-narration" : "pv-tl-narration--static";
  return m(`div.${captionClass}.markdown-content`, m.trust(renderMarkdown(narration)));
}

function renderExpandedStepBody(
  step: StepNode,
  toolResults: Map<string, ToolResultEvent>,
  chatId: string,
  draft: AssistantMessageEvent | null,
): m.Vnode {
  // One run over all the step's events, so a sequence of tool calls reads as a
  // single row of chips rather than one row per event.
  const children = renderAssistantRun(draft === null ? step.events : [...step.events, draft], toolResults, chatId);
  // The revealed work sits flush under its title -- no indent and no left rule.
  // The stretch's line already runs down the left of every node, so a second
  // vertical line inside an opened step would read as a nested timeline that is
  // not one. Its p and tool-block child rules stay in style.css.
  return m("div", { class: "pv-expanded markdown-content mt-2.5 text-(length:--font-size-body)" }, children);
}

export function ProgressBlock(): m.Component<ProgressBlockAttrs> {
  // Per-step expand state lives in the shared in-memory expansion store, keyed
  // by section id + ticket_id: component state would be lost every time the
  // virtualized window unmounts and remounts this block (collapsing what the
  // user opened, and snapping the row height -- a visible jump). The section id
  // in the key keeps a carryover step rendered in two turns independent.
  let blockKeyPrefix = "";

  function stepKey(ticket_id: string): string {
    return `step:${blockKeyPrefix}:${ticket_id}`;
  }

  function renderStepNode(
    step: StepNode,
    is_last: boolean,
    toolResults: Map<string, ToolResultEvent>,
    chatId: string,
    draft: AssistantMessageEvent | null,
  ): m.Vnode {
    const canExpand = step.events.length > 0;
    const isExpanded = isBlockExpanded(stepKey(step.ticket_id));
    // The status/step modifiers are bare markers (the status look is resolved
    // in code -- see timelineTitleClass and statusIcon); the padding spaces the
    // run of steps, so the last one has none.
    const nodeClasses = [
      "pv-tl-node",
      `pv-tl-node--${step.status}`,
      "pv-tl-node--step",
      is_last ? "pv-tl-node--last pb-0" : "pb-[18px]",
      TIMELINE_NODE_CLASS,
    ].join(" ");

    return m("div", { class: nodeClasses, key: `step-${step.ticket_id}` }, [
      renderTimelineBullet(statusIcon(step.status, step.is_frontier)),
      m("div", { class: TIMELINE_BODY_CLASS }, [
        m(
          "button",
          {
            type: "button",
            class: timelineTitleClass(titleTone(step)),
            disabled: !canExpand,
            onclick: canExpand ? () => toggleBlockExpanded(stepKey(step.ticket_id)) : undefined,
          },
          [
            renderTimelineTitleText(step.title),
            canExpand
              ? m(
                  "span",
                  { class: `${TIMELINE_CHEVRON_CLASS} ${timelineChevronStateClass(isExpanded)}` },
                  m.trust("&rsaquo;"),
                )
              : null,
          ],
        ),
        renderStepCaption(step, isExpanded, draft),
        isExpanded
          ? m("div", { class: "pv-tl-expanded mt-1.5" }, renderExpandedStepBody(step, toolResults, chatId, draft))
          : null,
      ]),
    ]);
  }

  return {
    view(vnode) {
      const { items, trailing_reply, toolResults, chatId, id, liveStepId, replyDraft } = vnode.attrs;
      blockKeyPrefix = id ?? "";
      const isHead = (index: number): boolean => vnode.attrs.isStretchHead === true && index === 0;
      const draft = liveStepId != null && replyDraft !== undefined ? replyDraft() : null;

      // Consecutive nodes on the timeline (steps and handoffs) stand together as one run of steps.
      const runs: { key: string; nodes: TimelineItem[] }[] = [];
      const pieces: (m.Vnode | { run: number })[] = [];
      items.forEach((item, index) => {
        if (item.kind === "step" || item.kind === "handoff") {
          const previous = pieces[pieces.length - 1];
          if (previous !== undefined && "run" in previous) {
            runs[previous.run].nodes.push(item);
          } else {
            runs.push({ key: `steps-${item.kind === "step" ? item.step.ticket_id : item.node.key}`, nodes: [item] });
            pieces.push({ run: runs.length - 1 });
          }
          return;
        }
        if (item.kind === "ungrouped") {
          // Real work / prose that happened with no step open -- including a
          // step's ejected closing prose -- laid out exactly like a no-steps turn.
          pieces.push(
            m(
              "div",
              { class: `pv-ungrouped ${STRETCH_PIECES_CLASS}`, key: item.key },
              // The whole ungrouped run at once, so its tool calls merge into one
              // chip row the way a step's revealed work does.
              renderAssistantRun(item.events, toolResults, chatId, new Map(), new Map(), stretchRun(isHead(index))),
            ),
          );
          return;
        }
        if (item.kind === "permission") {
          // A permission request lifted out of its step, so the user can act on it
          // without opening the step: the permission-request card the renderer
          // produces (with its review button or, once the user decides, a
          // granted/denied verdict).
          pieces.push(
            m(
              "div",
              { class: "pv-permission", key: `perm-${item.event.event_id}` },
              renderPermissionItem(
                item.event,
                toolResults,
                chatId,
                item.resolutionsByRequestId,
                item.secretNotesByRequestId,
                isHead(index),
              ),
            ),
          );
          return;
        }
        // A chip that landed inside an open handoff.
        pieces.push(
          m(
            "div",
            { class: "pv-stophook", key: `${item.kind}-${item.event.event_id}` },
            renderUserMessage(item.event),
          ),
        );
      });

      const renderRun = (run: { key: string; nodes: TimelineItem[] }): m.Vnode =>
        m(
          "div",
          { class: `pv pv--timeline ${TIMELINE_STEPS_CLASS}`, key: run.key },
          run.nodes.map((item, index) => {
            const isLast = index === run.nodes.length - 1;
            if (item.kind === "step") {
              const stepDraft = item.step.ticket_id === liveStepId ? draft : null;
              return renderStepNode(item.step, isLast, toolResults, chatId, stepDraft);
            }
            if (item.kind !== "handoff") return null;
            // The chat's handoff to another agent, a node on the timeline like a step.
            return renderHandoffNode(item.node, chatId, toolResults, {
              isLast,
              expansionKey: `handoff:${blockKeyPrefix}:${item.node.key}`,
            });
          }),
        );

      return m(
        "div",
        {
          class: `progress-block ${STRETCH_PIECES_CLASS} text-(length:--font-size-body) leading-normal text-primary`,
          id,
        },
        [
          ...pieces.map((piece) => ("run" in piece ? renderRun(runs[piece.run]) : piece)),
          ...trailing_reply.map((ev) =>
            m(
              "div",
              { class: `pv-final ${STRETCH_PIECES_CLASS}`, key: `final-${ev.event_id}` },
              renderAssistantRun([ev], toolResults, chatId, new Map(), new Map(), stretchRun()),
            ),
          ),
        ],
      );
    },
  };
}
