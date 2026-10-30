import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import type { TranscriptEvent, SubagentMetadata } from "../models/Response";
import { describeRequestFailure } from "@imbue/workspace-ui/src/models/request-error";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { SeeDetails } from "@imbue/workspace-ui/src/components/SeeDetails";
import { ReportBugButton } from "@imbue/workspace-ui/src/components/FailureButtons";
import { openChatPage } from "../shell";
import { getShellConnection } from "../shellConnection";
import { plainFailureLinesOf, plainFailureSentence } from "./failure-lines";
import type { FailureLines } from "./failure-lines";
import { parseJsonMessage } from "@imbue/workspace-ui/src/models/ws-json";
import {
  buildConversationRows,
  isSubagentRunning,
  MESSAGE_LIST_CLASS,
  renderTranscriptSegments,
  type RowDescriptor,
} from "./conversation-rows";
import { createTranscriptScrollEngine } from "./transcript-scroll-engine";
import { TranscriptScrollbar } from "./TranscriptScrollbar";
import { badgeClass } from "@imbue/workspace-ui/src/components/Badge";
import { BADGE_PART } from "@imbue/workspace-ui/src/themes/parts";

interface SubagentViewAttrs {
  chatId: string;
  // The agent of the chat whose harness session the subagent is a session of.
  agentId: string;
  subagentSessionId: string;
}

interface SubagentEventsResponse {
  events: TranscriptEvent[];
  metadata: SubagentMetadata | null;
}

/** The chat app's route for one subagent session: the chat, the agent it ran under, the session. */
function subagentRoute(chatId: string, agentId: string, sessionId: string, leaf: "events" | "stream"): string {
  const parts = [chatId, "agents", agentId, "subagents", sessionId, leaf];
  return `/api/chats/${parts.map(encodeURIComponent).join("/")}`;
}

export function SubagentView(): m.Component<SubagentViewAttrs> {
  let events: TranscriptEvent[] = [];
  // Persistent dedup set so each live SSE delta is O(1), not an O(n) rebuild.
  const eventIds = new Set<string>();
  let metadata: SubagentMetadata | null = null;
  let loading = true;
  let loadingError: FailureLines | null = null;
  // The chat or its agent is gone, so there is nothing to load again.
  let isGone = false;
  let eventSource: EventSource | null = null;

  // Memoized rows. buildConversationRows walks the whole subagent transcript, so
  // it is recomputed only when the event set or idleness changes -- not on every
  // scroll redraw. The transcript is append-only here (no in-place upgrades, no
  // eviction), so the event count plus the idle flag is a sufficient cache key.
  let rowsCacheKey = "";
  let cachedRows: RowDescriptor[] = [];
  // Monotonic version for the engine's geometry cache, bumped with the rows.
  let rowsVersion = 0;

  // The same scroll engine as the main chat, with an empty virtual layer: the
  // whole subagent transcript is loaded, so the custom scrollbar is 100%
  // physical (pixel-space) and the fill planner has nothing to fetch. No
  // persistence key: a subagent view always opens at the live tail.
  const engine = createTranscriptScrollEngine({
    isVisible: () => true,
    dataSource: {
      getRows: () => cachedRows,
      getWindowEventIds: () => events.map((event) => event.event_id),
      getFirstOffset: () => 0,
      getTotalEvents: () => (loading ? null : events.length),
      getRenderVersion: () => rowsVersion,
      executeFill: () => Promise.resolve(),
    },
  });

  function addEvents(incoming: TranscriptEvent[]): boolean {
    let added = false;
    for (const event of incoming) {
      if (!eventIds.has(event.event_id)) {
        eventIds.add(event.event_id);
        events.push(event);
        added = true;
      }
    }
    return added;
  }

  async function fetchSubagentEvents(chatId: string, agentId: string, subagentSessionId: string): Promise<void> {
    loading = true;
    loadingError = null;
    isGone = false;

    try {
      const result = await m.request<SubagentEventsResponse>({
        method: "GET",
        url: apiUrl(subagentRoute(chatId, agentId, subagentSessionId, "events")),
      });
      events = [];
      eventIds.clear();
      addEvents(result.events);
      metadata = result.metadata ?? null;
      loading = false;
    } catch (error) {
      loading = false;
      const failure = describeRequestFailure(error, "read");
      isGone = failure.kind === "side_conversation_gone" || failure.kind === "chat_gone";
      loadingError = plainFailureLinesOf(failure, "This conversation couldn't be loaded", { requestKind: "read" });
    }
  }

  function connectToStream(chatId: string, agentId: string, subagentSessionId: string): void {
    if (eventSource !== null) {
      return;
    }

    const url = apiUrl(subagentRoute(chatId, agentId, subagentSessionId, "stream"));
    eventSource = new EventSource(url);

    eventSource.onmessage = (messageEvent: MessageEvent) => {
      const event = parseJsonMessage<TranscriptEvent>(messageEvent.data);
      if (event === null) {
        return;
      }
      if (addEvents([event])) {
        m.redraw();
      }
    };

    eventSource.onerror = () => {
      if (eventSource !== null) {
        eventSource.close();
        eventSource = null;
      }
    };
  }

  function disconnectFromStream(): void {
    if (eventSource !== null) {
      eventSource.close();
      eventSource = null;
    }
  }

  function renderWindowedList(chatId: string): m.Vnode {
    // A subagent has no server-derived activity_state, so derive idleness from
    // the transcript tail; idle settles the frontier spinner. It is part of the
    // cache key alongside the event count.
    const agentIsIdle = !isSubagentRunning(events);
    const renderKey = `${chatId}|${events.length}|${agentIsIdle ? 1 : 0}`;
    if (renderKey !== rowsCacheKey) {
      // Same transcript -> sections -> rows pipeline as the main chat, so the
      // subagent's conversation renders an identical progress timeline; only the
      // idle source differs (derived here rather than from activity_state).
      cachedRows = buildConversationRows(chatId, events, agentIsIdle);
      rowsVersion += 1;
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;

    const plan = engine.computeRenderPlan();
    return m("div", { class: "message-list-wrapper" }, [
      m(
        "div",
        { class: MESSAGE_LIST_CLASS },
        renderTranscriptSegments(rows, [
          { kind: "spacer", height: plan.topPadPx },
          { kind: "rows", startIndex: plan.startIndex, endIndex: plan.endIndex },
          { kind: "spacer", height: plan.bottomPadPx },
        ]),
      ),
    ]);
  }

  function load({ chatId, agentId, subagentSessionId }: SubagentViewAttrs): void {
    void fetchSubagentEvents(chatId, agentId, subagentSessionId).then(() => {
      if (loadingError === null) connectToStream(chatId, agentId, subagentSessionId);
      m.redraw();
    });
  }

  function renderLoadingError(attrs: SubagentViewAttrs, failure: FailureLines): m.Vnode {
    if (isGone) {
      return m("div", { class: "message-list-error flex flex-col items-center justify-center h-full gap-3" }, [
        m("p", { class: "message-list-error-text text-secondary" }, "This side conversation is no longer available."),
        m(Button, { sm: true, onclick: () => openChatPage(attrs.chatId) }, "Back to the chat"),
      ]);
    }
    return m("div", { class: "message-list-error flex flex-col items-center justify-center h-full gap-3 p-8" }, [
      m("p", { class: "message-list-error-text text-danger text-center" }, plainFailureSentence(failure)),
      m("div", { class: "flex items-center gap-2" }, [
        m(Button, { sm: true, extra: "message-list-reload", onclick: () => load(attrs) }, "Try again"),
        failure.isReportable ? m(ReportBugButton, { connection: getShellConnection(), sm: true }) : null,
      ]),
      m("div", { class: "text-secondary" }, m(SeeDetails, { details: failure.details })),
    ]);
  }

  return {
    oninit(vnode) {
      engine.setChat(null);
      load(vnode.attrs);
    },

    onremove() {
      disconnectFromStream();
      engine.detach();
    },

    view(vnode) {
      const { chatId } = vnode.attrs;
      const title = metadata?.description || "Sub-agent conversation";
      const agentType = metadata?.agent_type || "";

      const header = m(
        "header",
        { class: "app-header flex shrink-0 items-baseline gap-3 border-b border-default bg-page px-8 py-3.5" },
        [
          m("h1", { class: "app-header-title type-heading text-primary" }, title),
          agentType ? m("span", { class: badgeClass("neutral", { mono: true }), ...BADGE_PART }, agentType) : null,
        ],
      );

      let content: m.Vnode;

      if (loading) {
        content = m(
          "div",
          { class: "message-list-loading flex items-center justify-center h-full" },
          m("p", { class: "text-secondary" }, "Loading events..."),
        );
      } else if (loadingError !== null) {
        content = renderLoadingError(vnode.attrs, loadingError);
      } else if (events.length === 0) {
        content = m(
          "div",
          { class: "message-list-empty flex items-center justify-center h-full" },
          m("p", { class: "text-secondary" }, "No events yet."),
        );
      } else {
        content = renderWindowedList(chatId);
      }

      return m("div", { class: "app-content-wrapper flex-1 flex flex-col min-h-0" }, [
        header,
        m("div", { class: "subagent-transcript-area relative flex-1 min-h-0 flex flex-col" }, [
          m(
            "main",
            {
              class: "app-content transcript-scroll flex-1 overflow-y-auto bg-chat px-6 py-6",
              tabindex: 0,
              oncreate: (mainVnode: m.VnodeDOM) => {
                engine.afterRender(mainVnode.dom as HTMLElement);
              },
              onupdate: (mainVnode: m.VnodeDOM) => {
                engine.afterRender(mainVnode.dom as HTMLElement);
              },
            },
            content,
          ),
          m(TranscriptScrollbar, { engine }),
        ]),
        // No footer/message input -- read-only
      ]);
    },
  };
}
