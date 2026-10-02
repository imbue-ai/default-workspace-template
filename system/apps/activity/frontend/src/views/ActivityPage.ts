/**
 * The memory tab, top to bottom: a plain headline over the workspace's real limit; a bar of where the memory goes
 * (chats, apps, background) with the point where closing starts; how memory has gone over time and what was closed;
 * ways to free memory when it is tight; then the chats, the apps, and the background services, each opening to the processes it is made of. Stopping a chat asks
 * first and says what will happen. Every figure says where it came from in its details.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { badgeClass } from "@imbue/workspace-ui/src/components/Badge";
import { Modal, MODAL_MESSAGE_CLASS } from "@imbue/workspace-ui/src/components/Modal";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { REFRESH_INTERVAL_MS } from "../models/summary";
import type {
  ActivityItem,
  ActivitySummary,
  AppStopResult,
  ChatActionResult,
  RefreshFailure,
  SummaryState,
} from "../models/summary";
import {
  chatStateLine,
  formatBytes,
  formatKib,
  formatShare,
  harnessName,
  headlineFor,
  isStopSuggested,
  pressureNotice,
  programStateLine,
} from "./format";
import { HistoryChart } from "./HistoryChart";
import type { HistoryChartAttrs } from "./HistoryChart";
import { chatDraftFor, questionsFor, rowLabel } from "./questions";
import {
  CHEVRON_SIZE,
  CLOSING_LINE_FILL,
  CLOSING_TEXT,
  DETAILS_CLASS,
  SECTION_HEADING_CLASS,
  SECTION_STYLES,
  chipClass,
  disclosure,
  sectionAnchorId,
  sectionOf,
} from "./styles";
import type { Section } from "./styles";

export interface ActivityPageAttrs {
  readonly state: SummaryState;
  /** The latest refresh that failed after the page had loaded, so the figures shown may be out of date. */
  readonly refreshFailure: RefreshFailure | null;
  readonly nowMs: number;
  readonly onChatAction: (
    chatId: string,
    action: "stop" | "start",
    isInterruptConfirmed: boolean,
  ) => Promise<ChatActionResult>;
  /** Quit an app through the desktop: its windows close, and it starts again when next opened. */
  readonly onAppStop: (appName: string) => Promise<AppStopResult>;
  /** Draft ``text``, unsent, into the user's chat; false when there is no desktop to draft into. */
  readonly onAskInChat: (text: string) => boolean;
  readonly history: HistoryChartAttrs;
}

// How long the line saying a stop or start went through stays up.
const ACTION_MESSAGE_MS = 15_000;

interface PendingStop {
  readonly item: ActivityItem;
  // Whether the dialog warned that the chat is mid-turn, so confirming it agrees to the interruption.
  readonly isWorking: boolean;
  // Set when the chat started working after the page last read it.
  readonly hasStartedWorking: boolean;
}

function stopFor(item: ActivityItem): PendingStop {
  return { item, isWorking: item.state === "working", hasStartedWorking: false };
}

export function ActivityPage(): m.Component<ActivityPageAttrs> {
  const openDetails = new Set<string>();
  let isServicesOpen = false;
  let pendingStop: PendingStop | null = null;
  let pendingAppStop: ActivityItem | null = null;
  let isActionRunning = false;
  let actionMessage: string | null = null;
  let openQuestionId: string | null = null;
  let askMessage: string | null = null;

  function askInChat(attrs: ActivityPageAttrs, summary: ActivitySummary, question: string | null): void {
    askMessage = attrs.onAskInChat(chatDraftFor(summary, question))
      ? "Your question is drafted in your chat, with what this page shows. Nothing is sent until you press send."
      : "Open System Monitor from your workspace desktop to ask in chat.";
  }

  function questions(summary: ActivitySummary, attrs: ActivityPageAttrs): m.Vnode {
    const items = questionsFor(summary);
    const open = items.find((item) => item.id === openQuestionId) ?? null;
    return m("section", { class: "flex flex-col gap-3", "aria-label": "Questions about this page" }, [
      m("div", { class: "flex flex-wrap gap-2" }, [
        ...items.map((item) =>
          m(
            Button,
            {
              key: item.id,
              variant: "secondary",
              sm: true,
              quiet: true,
              selected: openQuestionId === item.id,
              extra: "activity-question",
              "aria-expanded": String(openQuestionId === item.id),
              onclick: () => {
                openQuestionId = openQuestionId === item.id ? null : item.id;
                askMessage = null;
              },
            },
            item.question,
          ),
        ),
        m(
          Button,
          {
            key: "ask-anything",
            variant: "ghost",
            sm: true,
            quiet: true,
            extra: "activity-ask",
            onclick: () => askInChat(attrs, summary, null),
          },
          "Ask something else in chat",
        ),
      ]),
      open === null
        ? null
        : m("div", { class: "flex flex-col gap-2 rounded-lg border border-default bg-surface-secondary p-4" }, [
            ...open.answer.map((paragraph) => m("p", { class: "m-0 max-w-[65ch] type-body text-primary" }, paragraph)),
            m(
              Button,
              {
                variant: "ghost",
                sm: true,
                quiet: true,
                extra: "activity-ask -ml-3 self-start",
                onclick: () => askInChat(attrs, summary, open.question),
              },
              "Still unsure? Ask in chat",
            ),
          ]),
      askMessage === null ? null : m("p", { class: "m-0 type-helper text-secondary", role: "status" }, askMessage),
    ]);
  }

  function toggle(id: string): void {
    if (openDetails.has(id)) openDetails.delete(id);
    else openDetails.add(id);
  }

  function disclose(id: string, closedLabel: string, openLabel: string): m.Vnode {
    return disclosure(openDetails.has(id), closedLabel, openLabel, () => toggle(id));
  }

  async function runAction(
    attrs: ActivityPageAttrs,
    item: ActivityItem,
    action: "stop" | "start",
    isInterruptConfirmed: boolean,
  ): Promise<void> {
    if (item.chat_id === null || isActionRunning) return;
    isActionRunning = true;
    actionMessage = null;
    m.redraw();
    let result: ChatActionResult;
    try {
      result = await attrs.onChatAction(item.chat_id, action, isInterruptConfirmed);
    } catch (error) {
      result = { kind: "failed", message: String(error) };
    } finally {
      isActionRunning = false;
    }
    switch (result.kind) {
      case "done":
        pendingStop = null;
        actionMessage =
          action === "stop"
            ? `Stopped "${item.name}". Freed about ${formatKib(item.rss_kib)}.`
            : `Started "${item.name}".`;
        break;
      case "started_working":
        pendingStop = { item, isWorking: true, hasStartedWorking: true };
        break;
      case "failed":
        pendingStop = null;
        actionMessage = `Couldn't ${action} "${item.name}": ${result.message}`;
        break;
    }
    clearActionMessageLater();
    m.redraw();
  }

  async function runAppStop(attrs: ActivityPageAttrs, item: ActivityItem): Promise<void> {
    if (item.app_name === null) return;
    // The dialog shows the row as it was when opened; a refresh since may have found the app already stopped. Any other
    // state (unknown for a moment, starting, gone from the list) goes to the backend, which checks again.
    const current =
      attrs.state.kind === "loaded" ? attrs.state.summary.apps.find((app) => app.item_id === item.item_id) : undefined;
    if (current !== undefined && ["STOPPED", "EXITED", "FATAL"].includes(current.state)) {
      pendingAppStop = null;
      actionMessage = `"${item.name}" has already stopped.`;
      clearActionMessageLater();
      return;
    }
    isActionRunning = true;
    actionMessage = null;
    m.redraw();
    const result = await attrs.onAppStop(item.app_name);
    isActionRunning = false;
    pendingAppStop = null;
    actionMessage =
      result.kind === "done"
        ? `Stopped "${item.name}". Freed about ${formatKib(item.rss_kib)}.`
        : `Couldn't stop "${item.name}": ${result.message}`;
    clearActionMessageLater();
    m.redraw();
  }

  function clearActionMessageLater(): void {
    const shownMessage = actionMessage;
    setTimeout(() => {
      if (actionMessage === shownMessage) {
        actionMessage = null;
        m.redraw();
      }
    }, ACTION_MESSAGE_MS);
  }

  function processTable(item: ActivityItem): m.Vnode {
    if (item.processes.length === 0) return m("div", { class: DETAILS_CLASS }, "No processes running.");
    return m(
      "div",
      { class: DETAILS_CLASS },
      m("table", { class: "w-full border-collapse tabular-nums" }, [
        m(
          "thead",
          m("tr", [
            m("th", { class: "pr-3 text-left font-medium text-secondary" }, "PID"),
            m("th", { class: "pr-3 text-left font-medium text-secondary" }, "Process"),
            m("th", { class: "pr-3 text-right font-medium text-secondary" }, "MiB"),
            m("th", { class: "pr-3 text-right font-medium text-secondary" }, "OOM adj"),
            m("th", { class: "text-left font-medium text-secondary" }, "Command"),
          ]),
        ),
        m(
          "tbody",
          item.processes.map((process) =>
            m("tr", { key: process.pid }, [
              m("td", { class: "pr-3 align-top" }, String(process.pid)),
              m("td", { class: "pr-3 align-top whitespace-nowrap" }, process.command_name),
              m("td", { class: "pr-3 text-right align-top" }, String(Math.round(process.rss_kib / 1024))),
              m("td", { class: "pr-3 text-right align-top" }, String(process.oom_score_adj)),
              m("td", { class: "align-top break-all" }, process.command_line),
            ]),
          ),
        ),
      ]),
    );
  }

  /** A row's action: Stop or Start where the page offers one (never in a preview, which changes nothing), else why
   * a running app the page offers no Stop for keeps running. */
  function rowAction(
    item: ActivityItem,
    isChat: boolean,
    isStopped: boolean,
    isPreview: boolean,
    attrs: ActivityPageAttrs,
  ): m.Children {
    const button = (variant: "ghost" | "secondary", label: string, onclick: () => void): m.Vnode =>
      m(Button, { variant, sm: true, disabled: isActionRunning, onclick }, label);
    if (!isPreview && isChat) {
      return isStopped
        ? button("ghost", "Start", () => runAction(attrs, item, "start", false))
        : button("secondary", "Stop", () => (pendingStop = stopFor(item)));
    }
    if (!isPreview && item.is_stoppable && item.state === "RUNNING") {
      return button("secondary", "Stop", () => (pendingAppStop = item));
    }
    if (item.always_on_reason !== null && item.state === "RUNNING") {
      return m("span", { class: badgeClass("neutral"), title: item.always_on_reason }, "Always on");
    }
    return null;
  }

  function itemRow(item: ActivityItem, summary: ActivitySummary, usedKib: number, attrs: ActivityPageAttrs): m.Vnode {
    const isFirstToClose = summary.likely_first_to_close?.item_id === item.item_id;
    const harness = harnessName(item.harness);
    const isChat = item.kind === "CHAT";
    const isAgent = isChat || item.kind === "HELPER_AGENT" || item.kind === "AGENT";
    const isStopped = isAgent ? item.state === "stopped" : item.state !== "RUNNING" && item.state !== "UNKNOWN";
    const subLine = isAgent ? chatStateLine(item, attrs.nowMs) : programStateLine(item);
    const action = rowAction(item, isChat, isStopped, summary.is_preview, attrs);
    const sharePercent = usedKib > 0 ? Math.min(100, (100 * item.rss_kib) / usedKib) : 0;
    const share = formatShare(item.rss_kib, usedKib);
    return m(
      "div",
      {
        key: item.item_id,
        class: "activity-item flex flex-col gap-2 border-b border-subtle py-3 last:border-b-0",
        role: "group",
        "aria-label": rowLabel(item, subLine, isFirstToClose, share),
        "data-item-id": item.item_id,
      },
      [
        m("div", { class: "flex items-center gap-4" }, [
          m("div", { class: "flex min-w-0 flex-1 flex-col gap-0.5" }, [
            m("div", { class: "flex flex-wrap items-center gap-2" }, [
              m(
                "span",
                { class: isStopped ? "type-body font-medium text-secondary" : "type-body font-medium text-primary" },
                item.name,
              ),
              harness === null ? null : m("span", { class: badgeClass("neutral") }, harness),
              isFirstToClose
                ? m("span", { class: chipClass("danger") }, "Likely closed first if memory runs out")
                : null,
            ]),
            m("span", { class: "type-helper text-secondary" }, subLine),
            disclose(`item:${item.item_id}`, "Details", "Hide details"),
          ]),
          m("div", { class: "flex w-36 shrink-0 flex-col items-end gap-1" }, [
            m(
              "span",
              { class: "type-body tabular-nums text-primary" },
              item.rss_kib > 0
                ? [formatKib(item.rss_kib), m("span", { class: "text-secondary" }, ` · ${share}`)]
                : "—",
            ),
            m("div", { class: "h-1 w-full overflow-hidden rounded-sm bg-surface-secondary", "aria-hidden": "true" }, [
              m("div", {
                class: `h-full rounded-sm ${SECTION_STYLES[sectionOf(item.kind)].fill}`,
                style: { width: `${item.rss_kib > 0 ? Math.max(1, sharePercent) : 0}%` },
              }),
            ]),
          ]),
          m("div", { class: "flex w-20 shrink-0 justify-end" }, action),
        ]),
        openDetails.has(`item:${item.item_id}`)
          ? m("div", { class: "flex flex-col gap-2" }, [
              processTable(item),
              isFirstToClose && summary.likely_first_to_close !== null
                ? m(
                    "p",
                    { class: "m-0 type-helper text-secondary" },
                    `A prediction: when memory runs out, ${summary.memory?.closer === "SYSTEM_LIMIT" ? "the system" : "the memory guard (earlyoom)"} ` +
                      "closes the process with the highest score: its memory plus its OOM adj in thousandths of the " +
                      `total it weighs. Right now that is ${summary.likely_first_to_close.command_name} ` +
                      `(PID ${summary.likely_first_to_close.pid}).`,
                  )
                : null,
            ])
          : null,
      ],
    );
  }

  function sectionTotalsKib(summary: ActivitySummary): Record<Section, number> {
    const totals: Record<Section, number> = { CHATS: 0, APPS: 0, BACKGROUND: 0 };
    for (const item of [...summary.chats, ...summary.apps, ...summary.services]) {
      totals[sectionOf(item.kind)] += item.rss_kib;
    }
    return totals;
  }

  /** The base every share on the page is out of: what is in use, or the summed process sizes when shared memory
   * (counted once in each process) makes them larger, so the bar, the sections and the rows always agree. */
  function shareBaseKib(summary: ActivitySummary): number {
    const totals = sectionTotalsKib(summary);
    const usedKib = summary.memory === null ? 0 : summary.memory.used_bytes / 1024;
    return Math.max(usedKib, totals.CHATS + totals.APPS + totals.BACKGROUND);
  }

  function jumpTo(section: Section): void {
    document.getElementById(sectionAnchorId(section))?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /** The bar of where memory goes and its legend, each legend entry a link to its section below. */
  function breakdown(summary: ActivitySummary): m.Children {
    const memory = summary.memory;
    if (memory === null) return null;
    const totals = sectionTotalsKib(summary);
    const usedKib = memory.used_bytes / 1024;
    const processKib = totals.CHATS + totals.APPS + totals.BACKGROUND;
    // The segments split what is in use: process sizes count shared memory once in each process, so they can sum
    // past it, and the memory no single process holds is left out of the bar (the legend states it).
    const usedPercent = (100 * memory.used_bytes) / memory.limit_bytes;
    const segmentScale = usedPercent / Math.max(usedKib, processKib, 1);
    const closeAtPercent = (100 * memory.closes_at_used_bytes) / memory.limit_bytes;
    const unheldKib = Math.max(0, usedKib - processKib);
    const sections: Section[] = ["CHATS", "APPS", "BACKGROUND"];
    return m(
      "div",
      {
        class: "activity-breakdown flex flex-col gap-2",
      },
      [
        m(
          "div",
          {
            class:
              "activity-memory-bar relative flex h-3.5 gap-0.5 rounded-md border border-default bg-surface-secondary",
            role: "img",
            "aria-label":
              `${formatBytes(memory.used_bytes)} of ${formatBytes(memory.limit_bytes)} in use: ` +
              sections.map((section) => `${SECTION_STYLES[section].label} ${formatKib(totals[section])}`).join(", "),
          },
          [
            ...sections
              .filter((section) => totals[section] > 0)
              .map((section) =>
                m("div", {
                  class: `h-full first:rounded-l-md ${SECTION_STYLES[section].fill}`,
                  style: { width: `${totals[section] * segmentScale}%` },
                }),
              ),
            m("div", {
              class: `absolute -top-1 -bottom-1 w-0.5 rounded-sm ${CLOSING_LINE_FILL}`,
              style: { left: `${closeAtPercent}%` },
            }),
          ],
        ),
        m("div", { class: "flex flex-wrap items-center gap-x-1 gap-y-1 type-helper text-secondary" }, [
          ...sections.map((section) =>
            m(
              Button,
              {
                variant: "ghost",
                sm: true,
                quiet: true,
                extra: "activity-legend",
                title: `Jump to ${SECTION_STYLES[section].label}`,
                onclick: () => jumpTo(section),
              },
              [
                m("span", { class: `inline-block h-2.5 w-2.5 rounded-sm ${SECTION_STYLES[section].fill}` }),
                `${SECTION_STYLES[section].label} ${formatKib(totals[section])}`,
              ],
            ),
          ),
          m("span", { class: `inline-flex items-center gap-1.5 px-2 ${CLOSING_TEXT}` }, [
            m("span", { class: `inline-block h-2.5 w-0.5 ${CLOSING_LINE_FILL}` }),
            "Closing starts here",
          ]),
          unheldKib > 0 ? m("span", { class: "px-2" }, `Not held by any one process: ${formatKib(unheldKib)}`) : null,
        ]),
      ],
    );
  }

  /** Memory that has stayed tight says so, with since when; one that eased is mentioned quietly for an hour. */
  function pressureBanner(summary: ActivitySummary): m.Children {
    const notice = pressureNotice(summary.pressure);
    if (notice === null) return null;
    return notice.isOngoing
      ? m(
          "div",
          { class: "activity-pressure flex flex-col gap-1 rounded-md bg-warning-surface px-3 py-2", role: "status" },
          [
            m("span", { class: "type-label text-primary" }, notice.title),
            m("span", { class: "type-helper text-primary" }, notice.body),
          ],
        )
      : m("p", { class: "activity-pressure m-0 type-helper text-secondary" }, `${notice.title}. ${notice.body}`);
  }

  function measuredDetails(summary: ActivitySummary): m.Children {
    const memory = summary.memory;
    if (memory === null) return null;
    const totals = sectionTotalsKib(summary);
    const processKib = totals.CHATS + totals.APPS + totals.BACKGROUND;
    return m("div", { class: "flex flex-col gap-2" }, [
      disclose("measured", "How this is measured", "Hide how this is measured"),
      openDetails.has("measured")
        ? m("div", { class: DETAILS_CLASS }, [
            m("div", `limit      ${Math.round(memory.limit_bytes / 1024 / 1024).toLocaleString()} MiB`),
            m("div", `in use     ${Math.round(memory.used_bytes / 1024 / 1024).toLocaleString()} MiB`),
            m("div", `source     ${memory.source_detail}`),
            m(
              "div",
              `closes at  ${Math.round(memory.closes_at_used_bytes / 1024 / 1024).toLocaleString()} MiB in use: ${memory.closing_detail}`,
            ),
            m(
              "div",
              `processes  ${formatKib(processKib)} summed; shared memory counts once in each process, so when the ` +
                "sum is larger than what is in use, every share is scaled to fit",
            ),
            m(
              "div",
              "shares     a row's or section's bar and percentage are its share of the memory in use, not of the limit",
            ),
            m(
              "div",
              "warnings   five readings in a row, a minute apart, at or above the getting-tight line, from the " +
                "history the chart draws (data/.state/activity/memory-history.tsv)",
            ),
            m(
              "div",
              `measured   ${new Date(summary.measured_at).toLocaleTimeString()}, refreshed every ${REFRESH_INTERVAL_MS / 1000} s while this window is visible`,
            ),
          ])
        : null,
    ]);
  }

  function suggestions(summary: ActivitySummary, attrs: ActivityPageAttrs): m.Children {
    if (summary.memory === null || summary.memory.status === "COMFORTABLE" || summary.is_preview) return null;
    const stoppable = summary.chats.filter((item) => isStopSuggested(item, attrs.nowMs));
    if (stoppable.length === 0) return null;
    return m("div", { class: "flex flex-col gap-2 rounded-lg border border-default bg-surface-secondary p-4" }, [
      m("span", { class: "type-label text-primary" }, "Ways to free up memory"),
      stoppable
        .sort((a, b) => b.rss_kib - a.rss_kib)
        .map((item) =>
          m("div", { key: item.item_id, class: "flex flex-wrap items-center justify-between gap-3" }, [
            m("div", { class: "flex flex-col" }, [
              m("span", { class: "type-body text-primary" }, `Stop "${item.name}"`),
              m(
                "span",
                { class: "type-helper text-secondary" },
                `${chatStateLine(item, attrs.nowMs)} · frees about ${formatKib(item.rss_kib)}`,
              ),
            ]),
            m(
              Button,
              {
                variant: "primary",
                sm: true,
                disabled: isActionRunning,
                onclick: () => (pendingStop = stopFor(item)),
              },
              "Stop",
            ),
          ]),
        ),
    ]);
  }

  function sectionHeading(section: Section, title: string, totalKib: number, usedKib: number): m.Children {
    return [
      m("h3", { class: `m-0 inline-flex items-center gap-2 ${SECTION_HEADING_CLASS}` }, [
        m("span", {
          class: `inline-block h-2.5 w-2.5 rounded-sm ${SECTION_STYLES[section].fill}`,
          "aria-hidden": "true",
        }),
        title,
      ]),
      m(
        "span",
        { class: "type-helper tabular-nums text-secondary" },
        `${formatKib(totalKib)} · ${formatShare(totalKib, usedKib)}`,
      ),
    ];
  }

  function group(
    section: Section,
    items: readonly ActivityItem[],
    summary: ActivitySummary,
    usedKib: number,
    attrs: ActivityPageAttrs,
    empty: string,
  ): m.Vnode {
    const total = items.reduce((sum, item) => sum + item.rss_kib, 0);
    return m("section", { id: sectionAnchorId(section), class: "flex scroll-mt-4 flex-col" }, [
      m(
        "div",
        { class: "flex items-baseline justify-between border-b border-default pb-1.5" },
        sectionHeading(section, SECTION_STYLES[section].label, total, usedKib),
      ),
      items.length === 0
        ? m("p", { class: "m-0 py-3 type-helper text-secondary" }, empty)
        : items.map((item) => itemRow(item, summary, usedKib, attrs)),
    ]);
  }

  /** The confirmation both stops share: what will happen, "Keep running", and the stop itself, focused so Enter
   * confirms it. Neither closes while the stop runs. */
  function confirmStop(
    title: string,
    message: string,
    confirmLabel: string,
    onConfirm: () => void,
    onClose: () => void,
  ): m.Children {
    const close = (): void => {
      if (!isActionRunning) onClose();
    };
    return m(
      Modal,
      {
        onDismiss: close,
        onEscape: close,
        title,
        card: { role: "dialog", "aria-modal": "true", "aria-label": title },
        actions: [
          m(
            "span",
            { oncreate: (vnode: m.VnodeDOM) => vnode.dom.querySelector("button")?.focus() },
            m(Button, { variant: "secondary", disabled: isActionRunning, onclick: close }, "Keep running"),
          ),
          m(
            Button,
            { variant: "primary", disabled: isActionRunning, onclick: onConfirm },
            isActionRunning ? "Stopping…" : confirmLabel,
          ),
        ],
      },
      m("p", { class: MODAL_MESSAGE_CLASS }, message),
    );
  }

  function stopDialog(attrs: ActivityPageAttrs): m.Children {
    if (pendingStop === null) return null;
    const { item, isWorking, hasStartedWorking } = pendingStop;
    const hasHelpers =
      attrs.state.kind === "loaded" && attrs.state.summary.chats.some((other) => other.kind === "HELPER_AGENT");
    return confirmStop(
      `Stop "${item.name}"?`,
      (hasStartedWorking
        ? "It started working since you opened this. Stopping now interrupts what it's doing. "
        : isWorking
          ? "It's working right now. Stopping interrupts what it's doing. "
          : "") +
        `It stops using memory (about ${formatKib(item.rss_kib)}). The conversation is kept, and the chat picks ` +
        "up where it left off when you message it." +
        (hasHelpers ? " Helper agents listed separately keep running; stopping a chat doesn't stop them." : ""),
      "Stop chat",
      () => runAction(attrs, item, "stop", isWorking),
      () => (pendingStop = null),
    );
  }

  function appStopDialog(attrs: ActivityPageAttrs): m.Children {
    if (pendingAppStop === null) return null;
    const item = pendingAppStop;
    return confirmStop(
      `Stop "${item.name}"?`,
      `Its windows close on every desktop, including those of anyone you've shared it with, and it stops using memory ` +
        `(about ${formatKib(item.rss_kib)}). It starts again the next time it's opened, so a pinned window of it, ` +
        "which stays open, may start it again. Anything it hadn't saved may be lost.",
      "Stop app",
      () => runAppStop(attrs, item),
      () => (pendingAppStop = null),
    );
  }

  return {
    view: ({ attrs }) => {
      const { state } = attrs;
      if (state.kind === "loading")
        return m("p", { class: "p-6 type-body text-secondary" }, "Reading what's running…");
      if (state.kind === "failed")
        return m("p", { class: "p-6 type-body text-danger-hover" }, `Couldn't read what's running. ${state.message}`);
      if (state.kind === "forbidden")
        return m(
          "p",
          { class: "p-6 type-body text-secondary" },
          "System Monitor is only available to the workspace's owner.",
        );
      const summary = state.summary;
      const usedKib = shareBaseKib(summary);
      // The likely first to close is flagged on its row, so a pick among the background services opens them.
      const isServicesShown =
        isServicesOpen || summary.services.some((item) => item.item_id === summary.likely_first_to_close?.item_id);
      const servicesSize = formatKib(summary.services.reduce((sum, item) => sum + item.rss_kib, 0));
      const headline = summary.memory === null ? null : headlineFor(summary.memory);
      return [
        m("div", { class: "flex flex-col gap-6" }, [
          headline === null
            ? m("h2", { class: "m-0 type-heading-lg text-primary" }, "Memory use couldn't be read.")
            : m("div", { class: "flex flex-col gap-2" }, [
                m("span", { class: `self-start ${chipClass(headline.tone)}` }, headline.badge),
                m("h2", { class: "m-0 type-heading-lg text-primary text-balance" }, headline.title),
                m("p", { class: "m-0 type-body text-secondary" }, headline.body),
              ]),
          pressureBanner(summary),
          summary.is_preview
            ? m(
                "p",
                { class: "m-0 rounded-md bg-warning-surface px-3 py-2 type-helper text-primary", role: "status" },
                "This is a preview of a proposed change to System Monitor. It shows the live workspace but can't stop anything in it.",
              )
            : null,
          attrs.refreshFailure === null
            ? null
            : m(
                "p",
                { class: "m-0 rounded-md bg-warning-surface px-3 py-2 type-helper text-primary", role: "status" },
                `Couldn't refresh since ${new Date(attrs.refreshFailure.since).toLocaleTimeString()}, so these figures ` +
                  `may be out of date. ${attrs.refreshFailure.message}`,
              ),
          breakdown(summary),
          measuredDetails(summary),
          m(HistoryChart, attrs.history),
          questions(summary, attrs),
          actionMessage === null
            ? null
            : m("p", { class: "m-0 type-body text-primary", role: "status" }, actionMessage),
          suggestions(summary, attrs),
          summary.are_chat_names_known
            ? null
            : m(
                "p",
                { class: "m-0 type-helper text-secondary" },
                "Chat names couldn't be read right now, so running agents are listed by their own names.",
              ),
          group("CHATS", summary.chats, summary, usedKib, attrs, "No chats are running."),
          summary.are_programs_known
            ? null
            : m(
                "p",
                { class: "m-0 type-helper text-secondary" },
                "App and service states couldn't be read right now; their memory is counted under workspace plumbing.",
              ),
          group("APPS", summary.apps, summary, usedKib, attrs, "No apps are registered."),
          m("section", { id: sectionAnchorId("BACKGROUND"), class: "flex scroll-mt-4 flex-col" }, [
            m("div", { class: "flex items-center justify-between border-b border-default pb-1.5" }, [
              m("h3", { class: `m-0 inline-flex items-center gap-2 ${SECTION_HEADING_CLASS}` }, [
                m("span", {
                  class: `inline-block h-2.5 w-2.5 rounded-sm ${SECTION_STYLES.BACKGROUND.fill}`,
                  "aria-hidden": "true",
                }),
                `Background services · ${summary.services.length}`,
              ]),
              m(
                Button,
                {
                  variant: "ghost",
                  sm: true,
                  quiet: true,
                  extra: "activity-services-toggle tabular-nums",
                  "aria-expanded": String(isServicesShown),
                  "aria-label": `${isServicesShown ? "Hide" : "Show"} background services, ${servicesSize}`,
                  onclick: () => (isServicesOpen = !isServicesShown),
                },
                [
                  servicesSize,
                  m.trust(icon(isServicesShown ? "chevron-down" : "chevron-right", { size: CHEVRON_SIZE })),
                ],
              ),
            ]),
            m(
              "p",
              { class: "m-0 pt-2 type-helper text-secondary" },
              "These keep the workspace running: backups, sharing, the desktop itself. You can't stop them here.",
            ),
            isServicesShown ? summary.services.map((item) => itemRow(item, summary, usedKib, attrs)) : null,
          ]),
          summary.notes.length === 0
            ? null
            : m(
                "div",
                { class: DETAILS_CLASS },
                summary.notes.map((note) => m("div", note)),
              ),
          m(
            "p",
            { class: "m-0 type-helper text-secondary" },
            "Tip: right-click anything on this page and choose Explain to ask about it in chat.",
          ),
        ]),
        stopDialog(attrs),
        appStopDialog(attrs),
      ];
    },
  };
}
