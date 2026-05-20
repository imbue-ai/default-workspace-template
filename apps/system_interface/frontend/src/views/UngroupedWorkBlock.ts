/**
 * Fallback rendering for turns where the agent did not declare step records.
 * Tool calls are collapsed under an "Ungrouped work" header; text-only
 * assistant messages (the agent's actual prose) render below in full.
 *
 * Visually echoes the ProgressBlock timeline but with a single node and
 * a muted style signaling that proper step tracking was not used.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import type { TranscriptEvent } from "../models/Response";
import { renderAssistantMessageChildren } from "./message-renderers";

interface UngroupedWorkBlockAttrs {
  body_events: TranscriptEvent[];
  toolResults: Map<string, TranscriptEvent>;
  agentId: string;
}

export function UngroupedWorkBlock(): m.Component<UngroupedWorkBlockAttrs> {
  let expanded = false;

  return {
    view(vnode) {
      const { body_events, toolResults, agentId } = vnode.attrs;

      const assistantEvents = body_events.filter((e) => e.type === "assistant_message");
      if (assistantEvents.length === 0) return null;

      const hasToolCalls = assistantEvents.some((e) => e.tool_calls && e.tool_calls.length > 0);
      const textOnlyMessages = assistantEvents.filter((e) => !!e.text && !(e.tool_calls && e.tool_calls.length > 0));

      if (!hasToolCalls) {
        return m(
          "div.progress-block.progress-block--ungrouped",
          textOnlyMessages.map((ev) => m("div.pv-final", m(MarkdownContent, { content: ev.text ?? "" }))),
        );
      }

      const expandedChildren: m.Children[] = [];
      for (const e of assistantEvents) {
        expandedChildren.push(...renderAssistantMessageChildren(e, toolResults, agentId));
      }

      return m("div.progress-block.progress-block--ungrouped", [
        m("div.pv.pv--timeline", [
          m("div.pv-timeline-thread", { "aria-hidden": "true" }),
          m("div.pv-timeline-nodes", [
            m("div.pv-tl-node.pv-tl-node--ungrouped.pv-tl-node--last", [
              m(
                "div.pv-tl-bullet",
                m(
                  "svg.pv-icon.pv-icon--ungrouped",
                  { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none" },
                  m.trust(
                    '<circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1" stroke-dasharray="4 2"/>',
                  ),
                ),
              ),
              m("div.pv-tl-body", [
                m(
                  "button",
                  {
                    type: "button",
                    class: "pv-tl-title",
                    onclick: () => {
                      expanded = !expanded;
                    },
                  },
                  [
                    "Ungrouped work",
                    m("span", { class: `pv-chev ${expanded ? "pv-chev--open" : ""}` }, m.trust("&rsaquo;")),
                  ],
                ),
                expanded ? m("div.pv-tl-expanded", m("div.pv-expanded.markdown-content", expandedChildren)) : null,
              ]),
            ]),
          ]),
        ]),
        textOnlyMessages.length > 0
          ? textOnlyMessages.map((ev) => m("div.pv-final", m(MarkdownContent, { content: ev.text ?? "" })))
          : null,
      ]);
    },
  };
}
