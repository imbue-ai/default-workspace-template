import m from "mithril";
import { apiUrl } from "../base-path";
import {
  applyEnrichmentSnapshot,
  getEnrichmentForAgent,
  type TranscriptEvent,
  type StepEnrichment,
  type SubagentMetadata,
} from "../models/Response";
import { parseJsonMessage } from "../models/ws-json";
import { computeVisibleWindow } from "../models/virtualWindow";
import { createRowMeasurer, OVERSCAN_PX } from "./row-measurement";
import { buildConversationRows, isSubagentRunning, type RowDescriptor } from "./conversation-rows";

interface SubagentViewAttrs {
  agentId: string;
  subagentSessionId: string;
}

interface SubagentEventsResponse {
  events: TranscriptEvent[];
  metadata: SubagentMetadata | null;
  // The subagent's own steps, scoped to its session, so its conversation
  // renders a real progress timeline with the same code as the main chat.
  step_enrichment?: Record<string, StepEnrichment>;
}

export function SubagentView(): m.Component<SubagentViewAttrs> {
  let events: TranscriptEvent[] = [];
  // Persistent dedup set so each live SSE delta is O(1), not an O(n) rebuild.
  const eventIds = new Set<string>();
  let metadata: SubagentMetadata | null = null;
  let loading = true;
  let loadingError: string | null = null;
  let eventSource: EventSource | null = null;

  // Virtualization state (a subagent transcript is bounded but can still be
  // large; only the viewport window is rendered to the DOM).
  let scrollEl: HTMLElement | null = null;
  let viewportHeight = 0;
  let scrollTop = 0;
  const rowMeasurer = createRowMeasurer();
  let userScrolledUp = false;
  let previousScrollTop = 0;
  let viewportResizeObserver: ResizeObserver | null = null;
  // Memoized rows. buildConversationRows walks the whole subagent transcript, so
  // it is recomputed only when the inputs change -- not on every scroll redraw.
  // The transcript is append-only here (no in-place upgrades, no eviction), so
  // the event count plus the enrichment version (bumped whenever a scoped step
  // snapshot is applied) is a sufficient cache key.
  let rowsCacheKey = "";
  let cachedRows: RowDescriptor[] = [];
  // Bumped on each applied step-enrichment snapshot so the row cache rebuilds
  // when steps change (the scoped enrichment table has no render version of its
  // own, unlike the main view's store).
  let enrichmentVersion = 0;

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

  async function fetchSubagentEvents(agentId: string, subagentSessionId: string): Promise<void> {
    loading = true;
    loadingError = null;

    try {
      const result = await m.request<SubagentEventsResponse>({
        method: "GET",
        url: apiUrl(
          `/api/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentSessionId)}/events`,
        ),
      });
      events = [];
      eventIds.clear();
      addEvents(result.events);
      metadata = result.metadata ?? null;
      applyEnrichmentSnapshot(agentId, result.step_enrichment, subagentSessionId);
      enrichmentVersion++;
      loading = false;
    } catch (error) {
      loading = false;
      loadingError = (error as Error).message ?? String(error);
    }
  }

  function connectToStream(agentId: string, subagentSessionId: string): void {
    if (eventSource !== null) {
      return;
    }

    const url = apiUrl(
      `/api/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentSessionId)}/stream`,
    );
    eventSource = new EventSource(url);

    eventSource.onmessage = (messageEvent: MessageEvent) => {
      // A malformed frame must not throw out of the handler -- drop it and keep listening.
      const raw = parseJsonMessage<{ type?: string }>(messageEvent.data);
      if (raw === null) {
        return;
      }
      // A step_enrichment message (tagged with this subagent's session id by
      // the backend) is a full enrichment snapshot, not a transcript event --
      // replace this subagent's table and redraw.
      if (raw.type === "step_enrichment") {
        const snapshot = raw as { enrichment?: Record<string, StepEnrichment> };
        applyEnrichmentSnapshot(agentId, snapshot.enrichment, subagentSessionId);
        enrichmentVersion++;
        m.redraw();
        return;
      }
      const event = raw as TranscriptEvent;
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

  function applyScrollPosition(element: HTMLElement): void {
    if (!userScrolledUp) {
      element.scrollTop = element.scrollHeight;
      scrollTop = element.scrollTop;
      previousScrollTop = element.scrollTop;
    }
  }

  function handleScrollEvent(event: Event): void {
    const element = event.target as HTMLElement;
    const currentScrollTop = element.scrollTop;
    const didScrollUp = currentScrollTop < previousScrollTop;
    previousScrollTop = currentScrollTop;
    scrollTop = currentScrollTop;
    if (didScrollUp) {
      userScrolledUp = true;
      return;
    }
    if (element.scrollHeight - element.scrollTop - element.clientHeight < 40) {
      userScrolledUp = false;
    }
  }

  // Refresh the cached viewport height and schedule a measure pass; the
  // measure/cache mechanics live in the shared row measurer.
  function scheduleMeasure(): void {
    if (scrollEl !== null) {
      viewportHeight = scrollEl.clientHeight;
    }
    rowMeasurer.scheduleMeasure(() => scrollEl);
  }

  function renderWindowedList(agentId: string, subagentSessionId: string): m.Vnode {
    // A subagent has no server-derived activity_state, so derive idleness from
    // the transcript tail. Idle settles the frontier spinner, so it is part of
    // the cache key alongside the event count and the enrichment version.
    const agentIsIdle = !isSubagentRunning(events);
    const renderKey = `${events.length}|${enrichmentVersion}|${agentIsIdle ? 1 : 0}`;
    if (renderKey !== rowsCacheKey) {
      // Same section -> rows pipeline as the main chat, so the subagent's
      // conversation renders an identical progress timeline; only the enrichment
      // scope (this subagent's session) and the idle source differ.
      const enrichment = getEnrichmentForAgent(agentId, subagentSessionId);
      cachedRows = buildConversationRows(agentId, events, enrichment, agentIsIdle);
      rowMeasurer.prune(new Set(cachedRows.map((row) => row.key)));
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;
    const getHeight = (index: number): number => rowMeasurer.getHeight(rows[index].key) ?? rows[index].estimate;
    const windowResult = computeVisibleWindow({
      count: rows.length,
      getHeight,
      scrollTop,
      viewportHeight: viewportHeight > 0 ? viewportHeight : (scrollEl?.clientHeight ?? 2000),
      overscanPx: OVERSCAN_PX,
    });

    const visibleRows: m.Children[] = [];
    visibleRows.push(m("div", { key: "__spacer_top", style: `height: ${windowResult.topPad}px` }));
    for (let i = windowResult.startIndex; i < windowResult.endIndex; i++) {
      visibleRows.push(rows[i].render());
    }
    visibleRows.push(m("div", { key: "__spacer_bottom", style: `height: ${windowResult.bottomPad}px` }));

    return m("div", { class: "message-list-wrapper" }, [
      m(
        "div",
        { class: "message-list mx-auto w-full max-w-(--width-message-column) flex flex-col py-6" },
        visibleRows,
      ),
    ]);
  }

  return {
    oninit(vnode) {
      const { agentId, subagentSessionId } = vnode.attrs;
      fetchSubagentEvents(agentId, subagentSessionId).then(() => {
        connectToStream(agentId, subagentSessionId);
      });
    },

    onremove() {
      disconnectFromStream();
      if (viewportResizeObserver !== null) {
        viewportResizeObserver.disconnect();
        viewportResizeObserver = null;
      }
      scrollEl = null;
    },

    view(vnode) {
      const { agentId, subagentSessionId } = vnode.attrs;
      const title = metadata?.description || "Sub-agent conversation";
      const agentType = metadata?.agent_type || "";

      const header = m("header", { class: "app-header" }, [
        m("h1", { class: "app-header-title" }, title),
        agentType ? m("span", { class: "app-header-model-badge" }, agentType) : null,
      ]);

      let content: m.Vnode;

      if (loading) {
        content = m(
          "div",
          { class: "message-list-loading flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "Loading events..."),
        );
      } else if (loadingError) {
        content = m(
          "div",
          { class: "message-list-error flex items-center justify-center h-full" },
          m("p", { class: "text-red-500" }, `Error: ${loadingError}`),
        );
      } else if (events.length === 0) {
        content = m(
          "div",
          { class: "message-list-empty flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "No events yet."),
        );
      } else {
        content = renderWindowedList(agentId, subagentSessionId);
      }

      return m("div", { class: "app-content-wrapper flex-1 flex flex-col min-h-0" }, [
        header,
        m(
          "main",
          {
            class: "app-content flex-1 overflow-y-auto px-8 py-6",
            onscroll: handleScrollEvent,
            oncreate: (mainVnode: m.VnodeDOM) => {
              scrollEl = mainVnode.dom as HTMLElement;
              viewportHeight = scrollEl.clientHeight;
              viewportResizeObserver = new ResizeObserver(() => {
                if (scrollEl !== null && scrollEl.clientHeight !== viewportHeight) {
                  viewportHeight = scrollEl.clientHeight;
                  m.redraw();
                }
              });
              viewportResizeObserver.observe(scrollEl);
              applyScrollPosition(scrollEl);
              scheduleMeasure();
            },
            onupdate: (mainVnode: m.VnodeDOM) => {
              scrollEl = mainVnode.dom as HTMLElement;
              applyScrollPosition(scrollEl);
              scheduleMeasure();
            },
          },
          content,
        ),
        // No footer/message input -- read-only
      ]);
    },
  };
}
