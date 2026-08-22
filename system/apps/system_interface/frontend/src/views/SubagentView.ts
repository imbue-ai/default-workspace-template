import m from "mithril";
import { apiUrl } from "../base-path";
import type { TranscriptEvent, SubagentMetadata } from "../models/Response";
import { describeRequestError } from "../models/request-error";
import { parseJsonMessage } from "../models/ws-json";
import { buildConversationRows, isSubagentRunning, renderVirtualRows, type RowDescriptor } from "./conversation-rows";
import { resolveSelectionRowIndices } from "./scroll-selection";
import { createTranscriptScroll } from "./transcript-scroll";
import { createTranscriptVirtualizer } from "./transcriptVirtualizer";
import { createRowMeasureScheduler, createRowMeasurementStore } from "./rowMeasurement";

interface SubagentViewAttrs {
  agentId: string;
  subagentSessionId: string;
}

interface SubagentEventsResponse {
  events: TranscriptEvent[];
  metadata: SubagentMetadata | null;
}

export function SubagentView(): m.Component<SubagentViewAttrs> {
  let events: TranscriptEvent[] = [];
  // Persistent dedup set so each live SSE delta is O(1), not an O(n) rebuild.
  const eventIds = new Set<string>();
  let metadata: SubagentMetadata | null = null;
  let loading = true;
  let loadingError: string | null = null;
  let eventSource: EventSource | null = null;

  // Virtualization: only the viewport window (plus any selected rows) is rendered.
  // The scroll-follow machinery -- tail following, native-anchoring stability and
  // the drag/resize lifecycle -- lives in the shared controller.
  const scroll = createTranscriptScroll();
  const measurements = createRowMeasurementStore();
  // Memoized rows. buildConversationRows walks the whole subagent transcript, so
  // it is recomputed only when the event set or idleness changes -- not on every
  // scroll redraw. The transcript is append-only here (no in-place upgrades, no
  // eviction), so the event count plus the idle flag is a sufficient cache key.
  let rowsCacheKey = "";
  let cachedRows: RowDescriptor[] = [];
  // Row key -> index in cachedRows, for resolving a selection's DOM rows to pin.
  let cachedKeyToIndex = new Map<string, number>();
  // Row indices a live selection touches, read by the virtualizer's range extractor.
  let pinnedRowIndices: number[] = [];
  // The whole subagent transcript is loaded at once, so there is no unloaded
  // history to reserve space for -- both reserves stay zero and the arrangement is
  // otherwise identical to the main chat, which is what keeps the two from drifting.
  const virtualizer = createTranscriptVirtualizer({
    getScrollElement: () => scroll.scrollEl,
    getCount: () => cachedRows.length,
    getRowKey: (index) => cachedRows[index]?.key ?? String(index),
    estimateSize: (index) => {
      const row = cachedRows[index];
      if (row === undefined) {
        return 0;
      }
      return measurements.heightFor(row.key) ?? row.estimate;
    },
    getPaddingStart: () => 0,
    getPaddingEnd: () => 0,
    getPinnedIndices: () => pinnedRowIndices,
    isEnabled: () => true,
  });

  // The frame-debounced measure pass, shared with the main chat.
  const measureScheduler = createRowMeasureScheduler({
    store: measurements,
    getListElement: () => scroll.scrollEl?.querySelector(".message-list") ?? null,
    reportHeight: (rowKey, height) => {
      const index = cachedKeyToIndex.get(rowKey);
      if (index !== undefined) {
        virtualizer.resizeRow(index, height);
      }
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
      loading = false;
    } catch (error) {
      loading = false;
      loadingError = describeRequestError(error);
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

  function renderWindowedList(agentId: string): m.Vnode {
    // A subagent has no server-derived activity_state, so derive idleness from
    // the transcript tail; idle settles the frontier spinner. It is part of the
    // cache key alongside the event count.
    const agentIsIdle = !isSubagentRunning(events);
    const renderKey = `${agentId}|${events.length}|${agentIsIdle ? 1 : 0}`;
    if (renderKey !== rowsCacheKey) {
      // Same transcript -> sections -> rows pipeline as the main chat, so the
      // subagent's conversation renders an identical progress timeline; only the
      // idle source differs (derived here rather than from activity_state).
      cachedRows = buildConversationRows(agentId, events, agentIsIdle);
      cachedKeyToIndex = new Map(cachedRows.map((row, index) => [row.key, index]));
      measurements.prune(new Set(cachedRows.map((row) => row.key)));
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;

    // A live selection's rows stay mounted even when the viewport moves far away,
    // so scrolling or streaming past them does not collapse the selection. Only
    // those rows are held, not the ones in between, so there is no distance cap.
    pinnedRowIndices = resolveSelectionRowIndices(scroll.scrollEl, cachedKeyToIndex);

    virtualizer.sync();
    const items = virtualizer.getVirtualItems();

    return m("div", { class: "message-list-wrapper" }, [
      m(
        "div",
        { class: "message-list mx-auto w-full max-w-(--width-message-column) flex flex-col py-6" },
        renderVirtualRows(rows, items, virtualizer.getTrailingSpace()),
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
      scroll.detach();
      virtualizer.unmount();
    },

    view(vnode) {
      const { agentId } = vnode.attrs;
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
        content = renderWindowedList(agentId);
      }

      return m("div", { class: "app-content-wrapper flex-1 flex flex-col min-h-0" }, [
        header,
        m(
          "main",
          {
            class: "app-content flex-1 overflow-y-auto px-8 py-6",
            onscroll: (event: Event) => scroll.onScroll(event),
            onpointerdown: () => scroll.onPointerDown(),
            oncreate: (mainVnode: m.VnodeDOM) => {
              const element = mainVnode.dom as HTMLElement;
              scroll.attach(element);
              virtualizer.mount();
              scroll.applyScrollPosition(element);
              measureScheduler.schedule();
            },
            onupdate: (mainVnode: m.VnodeDOM) => {
              const element = mainVnode.dom as HTMLElement;
              scroll.attach(element);
              virtualizer.mount();
              scroll.applyScrollPosition(element);
              measureScheduler.schedule();
            },
          },
          content,
        ),
        // No footer/message input -- read-only
      ]);
    },
  };
}
