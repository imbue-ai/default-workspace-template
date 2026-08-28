/**
 * Progress block: timeline rendering of one section (one user turn).
 *
 * The section is a flat, ordered list of timeline items produced by the
 * transcript walk (see turn-grouping): step nodes, ungrouped work/prose runs
 * (rendered inline, thread-breaking -- including a step's ejected closing
 * prose), and chips. Step nodes carry their own grouped events; expanding a step
 * reveals that grouped work. The wrap-up reply renders below the timeline.
 *
 * This component renders structure it is given; it does no grouping or
 * ordering itself.
 */

import m from "mithril";
import { MarkdownContent, renderMarkdown } from "../markdown";
import type { ToolResultEvent, AssistantMessageEvent } from "../models/Response";
import {
  renderAssistantMessage,
  renderAssistantMessageChildren,
  renderPermissionItem,
  renderUserMessage,
} from "./message-renderers";
import type { StepNode, StepStatus, TimelineItem } from "./turn-grouping";
import { statusDoneIcon, statusPendingIcon, statusRingIcon } from "./components/icons";

interface ProgressBlockAttrs {
  /** Timeline items in transcript order (steps, ungrouped runs, chips). */
  items: TimelineItem[];
  /** The wrap-up reply, rendered below the timeline. */
  trailing_reply: AssistantMessageEvent[];
  /** Prebuilt tool_call_id -> tool_result map for the whole stream (skill
   *  expansions already folded in). Lookups by id work even though a section
   *  only references a subset. */
  toolResults: Map<string, ToolResultEvent>;
  agentId: string;
  /** Optional DOM id for the root, so a virtualized list can measure this
   *  block's height by querying ``.message-list > [id]``. */
  id?: string;
}

/* Styling.
 * Utilities in the markup; the pv-* class names stay as bare markers. What
 * remains in style.css for this view: the narration shimmer (a keyframe +
 * background-clip machine), the narration/expanded markdown-child rules
 * (rendered content), and the expanded panel's tool-block override (a
 * contextual rule over shared markup). */

/** The step title button: a reset button carrying the row's typography. All
 *  statuses share this one de-emphasized look (medium weight, soft color);
 *  the status is carried by the bullet icon instead. */
const TITLE_CLASS =
  "pv-tl-title inline-flex cursor-pointer items-center appearance-none border-0 bg-transparent p-0 text-left " +
  "text-(length:--font-size-body) leading-[1.4] font-medium text-secondary disabled:cursor-default";

/** The expand chevron beside the title. text-[18px]: icon glyph, sized
 *  independently of the text scale (and deliberately not text-lg, whose
 *  line-height would reflow the row). */
const CHEV_CLASS = "pv-chev ml-1.5 inline-block text-[18px] font-normal transition-transform duration-150 ease-[ease]";

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
    return m(
      "span",
      { class: "pv-icon pv-icon--active inline-flex h-4 w-4 shrink-0 items-center justify-center text-accent" },
      m("span.spinner.spinner--sm.spinner--current"),
    );
  }
  return m.trust(statusPendingIcon());
}

/** Sub-caption under the step title:
 *   - done + summary -> the close summary
 *   - active + narration (and not expanded) -> latest in-step narration
 *   - otherwise nothing. */
function renderStepCaption(step: StepNode, isExpanded: boolean): m.Vnode | null {
  if (step.status === "done") {
    return step.summary
      ? m(
          "div",
          { class: "pv-tl-summary mt-[3px] pl-0.5 text-(length:--font-size-body) leading-normal text-secondary" },
          step.summary,
        )
      : null;
  }
  if (isExpanded) return null;
  if (!step.narration) return null;
  // The narration look (muted italic; the frontier one's shimmer) stays in
  // style.css -- it styles markdown-rendered children and the shimmer is a
  // keyframe machine.
  const captionClass = step.is_frontier ? "pv-tl-narration" : "pv-tl-narration--static";
  return m(`div.${captionClass}.markdown-content`, m.trust(renderMarkdown(step.narration)));
}

function renderExpandedStepBody(step: StepNode, toolResults: Map<string, ToolResultEvent>, agentId: string): m.Vnode {
  const children: m.Children[] = [];
  for (const e of step.events) {
    children.push(...renderAssistantMessageChildren(e, toolResults, agentId));
  }
  // The subtle indent + left rule containing the revealed work; its p and
  // tool-block child rules stay in style.css.
  return m(
    "div",
    {
      class: "pv-expanded markdown-content mt-2.5 border-l-2 border-subtle py-1 pl-3.5 text-(length:--font-size-body)",
    },
    children,
  );
}

export function ProgressBlock(): m.Component<ProgressBlockAttrs> {
  // Per-step expand state, keyed by ticket_id. Each section mounts its own
  // ProgressBlock instance (keyed by section), so a carryover step rendered in
  // two turns holds independent expand state with no collision.
  const expanded = new Set<string>();

  function toggle(ticket_id: string): void {
    if (expanded.has(ticket_id)) expanded.delete(ticket_id);
    else expanded.add(ticket_id);
  }

  function renderStepNode(
    step: StepNode,
    is_last: boolean,
    toolResults: Map<string, ToolResultEvent>,
    agentId: string,
  ): m.Vnode {
    const canExpand = step.events.length > 0;
    const isExpanded = expanded.has(step.ticket_id);
    // The status/step modifiers are bare markers (the status look is resolved
    // in code -- see TITLE_CLASS and statusIcon); the padding caps the thread
    // on the last node.
    const nodeClasses = [
      "pv-tl-node",
      `pv-tl-node--${step.status}`,
      "pv-tl-node--step",
      is_last ? "pv-tl-node--last pb-0" : "pb-[18px]",
      "relative flex items-start gap-3.5",
    ].join(" ");

    return m("div", { class: nodeClasses, key: `step-${step.ticket_id}` }, [
      // The bullet's opaque chat background masks the thread behind it.
      m(
        "div",
        { class: "pv-tl-bullet relative z-(--z-content) w-4 shrink-0 bg-chat py-px" },
        statusIcon(step.status, step.is_frontier),
      ),
      m("div", { class: "pv-tl-body min-w-0 flex-1" }, [
        m(
          "button",
          {
            type: "button",
            class: TITLE_CLASS,
            disabled: !canExpand,
            onclick: canExpand ? () => toggle(step.ticket_id) : undefined,
          },
          [
            step.title,
            canExpand
              ? m(
                  "span",
                  {
                    class: `${CHEV_CLASS} ${isExpanded ? "pv-chev--open rotate-90 text-primary" : "text-secondary"}`,
                  },
                  m.trust("&rsaquo;"),
                )
              : null,
          ],
        ),
        renderStepCaption(step, isExpanded),
        isExpanded
          ? m("div", { class: "pv-tl-expanded mt-1.5" }, renderExpandedStepBody(step, toolResults, agentId))
          : null,
      ]),
    ]);
  }

  return {
    view(vnode) {
      const { items, trailing_reply, toolResults, agentId, id } = vnode.attrs;

      // Index of the last step item, so only it gets the `--last` thread cap.
      let lastStepIdx = -1;
      for (let i = 0; i < items.length; i++) if (items[i].kind === "step") lastStepIdx = i;

      const timelineNodes: m.Children[] = items.map((item, idx) => {
        if (item.kind === "step") {
          return renderStepNode(item.step, idx === lastStepIdx, toolResults, agentId);
        }
        if (item.kind === "ungrouped") {
          // Real work / prose that happened with no step open -- including a
          // step's ejected closing prose: rendered inline as a thread-breaking
          // block, exactly like a no-steps turn. Top space as padding, not
          // margin, so the opaque chat background extends up and masks a bit
          // of the timeline thread above the run. z-[2]:
          // design-system-exception -- lifts it above the thread; the z scale
          // has no "content + 1" layer.
          return m(
            "div",
            { class: "pv-ungrouped relative z-[2] mb-3.5 bg-chat pt-1.5", key: item.key },
            item.events.map((e) => renderAssistantMessage(e, toolResults, agentId)),
          );
        }
        if (item.kind === "permission") {
          // A permission request lifted out of its step: rendered inline as a
          // thread-breaking block so it is always visible, as the
          // permission-request card the renderer produces (with its review button
          // or, once the user decides, a granted/denied verdict). The card
          // carries its own opaque surface, so no background here (it would
          // paint the shell's off-white over the pure-white transcript).
          // z-[2]: design-system-exception, as above.
          return m(
            "div",
            { class: "pv-permission relative z-[2] mt-1.5 mb-3.5", key: `perm-${item.event.event_id}` },
            renderPermissionItem(item.event, toolResults, agentId, item.resolutionsByRequestId),
          );
        }
        // A stop-hook chip woven into the timeline at the point the hook
        // fired; the opaque pure-white chat background masks the thread
        // behind it. z-[2]: design-system-exception, as above.
        return m(
          "div",
          { class: "pv-stophook relative z-[2] mt-1.5 mb-3.5 bg-chat", key: `chip-${item.event.event_id}` },
          renderUserMessage(item.event),
        );
      });

      return m(
        "div",
        {
          class: "progress-block mt-[18px] mb-[28px] text-(length:--font-size-body) leading-normal text-primary",
          id,
        },
        [
          m("div.pv.pv--timeline.relative", [
            // The vertical thread the bullets sit on; nodes mask it with their
            // own opaque backgrounds.
            m("div", {
              class: "pv-timeline-thread absolute top-2 bottom-0 left-[7.5px] w-px bg-default",
              "aria-hidden": "true",
            }),
            m("div.pv-timeline-nodes", timelineNodes),
          ]),
          trailing_reply.length > 0
            ? trailing_reply.map((ev) =>
                m(
                  "div",
                  { class: "pv-final mt-4 text-(length:--font-size-body) leading-normal text-primary" },
                  m(MarkdownContent, { content: ev.text ?? "", requestedAt: ev.timestamp }),
                ),
              )
            : null,
        ],
      );
    },
  };
}
