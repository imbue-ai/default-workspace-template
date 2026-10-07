/**
 * The wait line under the activity strip: the tasks the agent left running that will wake it when
 * they finish, as the chat app lists them on ``active_agent.background_tasks`` (oldest first).
 *
 * Collapsed, it says how many there are and how long the oldest has run; pressing it opens one row
 * per task with its description and elapsed time. A one-second timer keeps the elapsed text moving
 * while any task is pending, and stops when none is.
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { BackgroundTask } from "../models/Chats";

const TICK_MS = 1_000;

/** How long a task has run, from its start to ``now`` (epoch ms): "45 sec", "4 min", "2 hr 5 min". */
function formatElapsed(startedAt: string, now: number): string {
  const startedMs = Date.parse(startedAt);
  if (Number.isNaN(startedMs)) return "";
  const seconds = Math.max(0, Math.floor((now - startedMs) / 1000));
  if (seconds < 60) return `${seconds} sec`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder === 0 ? `${hours} hr` : `${hours} hr ${remainder} min`;
}

/** The collapsed line's words: how many tasks the agent is waiting on. */
function waitingSummary(count: number): string {
  return count === 1 ? "Waiting on a background task" : `Waiting on ${count} background tasks`;
}

function elapsedText(startedAt: string, now: number): m.Vnode | null {
  const elapsed = formatElapsed(startedAt, now);
  return elapsed === ""
    ? null
    : m("span", { class: "background-tasks-line__elapsed flex-none text-faint" }, `· ${elapsed}`);
}

export function BackgroundTasksLine(): m.Component<{ tasks: readonly BackgroundTask[] }> {
  let isExpanded = false;
  let tickTimer: number | null = null;

  const stopTicking = (): void => {
    if (tickTimer !== null) {
      window.clearInterval(tickTimer);
      tickTimer = null;
    }
  };

  return {
    onremove() {
      stopTicking();
    },
    view({ attrs }) {
      const { tasks } = attrs;
      if (tasks.length === 0) {
        stopTicking();
        return null;
      }
      if (tickTimer === null) tickTimer = window.setInterval(() => m.redraw(), TICK_MS);
      const now = Date.now();
      return m(
        "div",
        {
          class: "background-tasks-line flex flex-col gap-0.5 px-1 text-(length:--font-size-helper) text-secondary",
        },
        [
          m(
            "button",
            {
              type: "button",
              class: "background-tasks-line__summary flex min-w-0 items-center gap-2 text-left hover:text-primary",
              "aria-expanded": isExpanded ? "true" : "false",
              onclick: () => {
                isExpanded = !isExpanded;
              },
            },
            [
              // A notch wider than the activity dot so the dashes read; the negative margin keeps the labels aligned.
              m("span", {
                class:
                  "background-tasks-line__ring -mx-px size-2 flex-none rounded-full border-[1.5px] border-dashed " +
                  "border-accent",
              }),
              m("span", { class: "background-tasks-line__label min-w-0 truncate" }, waitingSummary(tasks.length)),
              elapsedText(tasks[0].started_at, now),
              m(
                "span",
                {
                  class: `background-tasks-line__chevron flex flex-none text-faint transition-transform duration-(--dur-base) ${
                    isExpanded ? "rotate-90" : ""
                  }`,
                },
                m.trust(icon("chevron-right", { size: 12 })),
              ),
            ],
          ),
          isExpanded
            ? m(
                "ul",
                { class: "background-tasks-line__tasks flex flex-col gap-0.5 pl-3.5" },
                tasks.map((task) =>
                  m("li", { key: task.id, class: "background-tasks-line__task flex min-w-0 items-center gap-2" }, [
                    m("span", { class: "background-tasks-line__description min-w-0 truncate" }, task.description),
                    elapsedText(task.started_at, now),
                  ]),
                ),
              )
            : null,
        ],
      );
    },
  };
}
