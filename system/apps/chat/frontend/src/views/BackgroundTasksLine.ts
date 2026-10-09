/**
 * The wait line under the activity strip: the background tasks the chat waits on, whose
 * completion will start the agent's next turn (the snapshot's ``active_agent.background_tasks``).
 *
 * Collapsed, it is one line: "Waiting on N background tasks · <how long the oldest has run>".
 * Pressed, it opens into one row per task, its description and how long it has run. The
 * backend decides which tasks are pending; this view only counts them and ticks their clocks,
 * once a second while it is on screen.
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { BackgroundTask } from "../models/Chats";

const TICK_MS = 1_000;

/** How long something has run, at the precision a glance wants: "42s", "4m 05s", "2h 07m". */
export function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1_000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) return `${totalMinutes}m ${String(totalSeconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(totalMinutes / 60)}h ${String(totalMinutes % 60).padStart(2, "0")}m`;
}

/** "Waiting on 1 background task", "Waiting on 3 background tasks". */
export function waitSummary(count: number): string {
  return `Waiting on ${count} background ${count === 1 ? "task" : "tasks"}`;
}

/** Epoch ms of a task's start, or null when the backend sent a time this browser cannot read. */
function startedAtMs(task: BackgroundTask): number | null {
  const parsed = Date.parse(task.started_at);
  return Number.isNaN(parsed) ? null : parsed;
}

function elapsedSince(startedMs: number | null, now: number): string | null {
  return startedMs === null ? null : formatElapsed(now - startedMs);
}

interface BackgroundTasksLineAttrs {
  tasks: readonly BackgroundTask[];
}

export function BackgroundTasksLine(): m.Component<BackgroundTasksLineAttrs> {
  let isExpanded = false;
  let tickTimer: number | null = null;

  const stopTicking = (): void => {
    if (tickTimer !== null) {
      window.clearInterval(tickTimer);
      tickTimer = null;
    }
  };

  return {
    onremove: stopTicking,
    view({ attrs }) {
      const { tasks } = attrs;
      if (tasks.length === 0) {
        stopTicking();
        return null;
      }
      if (tickTimer === null) tickTimer = window.setInterval(() => m.redraw(), TICK_MS);
      const now = Date.now();
      const starts = tasks.map(startedAtMs).filter((start): start is number => start !== null);
      const oldestElapsed = starts.length === 0 ? null : elapsedSince(Math.min(...starts), now);
      return m("div", { class: "background-tasks-line flex w-full flex-col", "data-expanded": String(isExpanded) }, [
        m(
          "button",
          {
            type: "button",
            class:
              "background-tasks-line__summary flex w-full items-center gap-2 px-1 text-left " +
              "text-(length:--font-size-helper) text-secondary hover:text-primary",
            "aria-expanded": String(isExpanded),
            onclick: () => {
              isExpanded = !isExpanded;
            },
          },
          [
            m("span", { class: "background-tasks-line__ring chat-rail-dot--dashed flex-none rounded-full" }),
            m("span", { class: "background-tasks-line__label truncate" }, waitSummary(tasks.length)),
            oldestElapsed === null
              ? null
              : m("span", { class: "background-tasks-line__elapsed flex-none text-faint" }, `· ${oldestElapsed}`),
            m(
              "span",
              { class: `flex-none text-faint transition-transform ${isExpanded ? "rotate-180" : ""}` },
              m.trust(icon("chevron-down", { size: 12 })),
            ),
          ],
        ),
        isExpanded
          ? m(
              "ul",
              { class: "background-tasks-line__tasks mt-1 flex flex-col gap-0.5 pl-6" },
              tasks.map((task) => {
                const elapsed = elapsedSince(startedAtMs(task), now);
                return m(
                  "li",
                  {
                    key: `${task.source}-${task.id}`,
                    class:
                      "background-tasks-line__task flex items-center gap-2 text-(length:--font-size-helper) text-secondary",
                  },
                  [
                    m(
                      "span",
                      { class: "background-tasks-line__description min-w-0 flex-1 truncate" },
                      task.description === "" ? "Background task" : task.description,
                    ),
                    elapsed === null
                      ? null
                      : m("span", { class: "background-tasks-line__task-elapsed flex-none text-faint" }, elapsed),
                  ],
                );
              }),
            )
          : null,
      ]);
    },
  };
}
