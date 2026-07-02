import m from "mithril";
import { apiUrl } from "../base-path";
import type { TranscriptEvent, SubagentMetadata } from "../models/Response";
import { parseJsonMessage } from "../models/ws-json";
import { computeVisibleWindow } from "../models/virtualWindow";
import { nextUserScrolledUp } from "../models/scrollFollow";
import { createRowMeasurer, OVERSCAN_PX } from "./row-measurement";
import { buildConversationRows, isSubagentRunning, type RowDescriptor } from "./conversation-rows";
import {
  captureTopAnchor,
  contentTopOfRow,
  resolveSelectionRowRange,
  SELECTION_PIN_MAX_GAP_ROWS,
  type ScrollAnchor,
} from "./scroll-selection";

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

  // Virtualization state (a subagent transcript is bounded but can still be
  // large; only the viewport window is rendered to the DOM).
  let scrollEl: HTMLElement | null = null;
  let viewportHeight = 0;
  let scrollTop = 0;
  const rowMeasurer = createRowMeasurer();
  let userScrolledUp = false;
  let previousScrollTop = 0;
  let viewportResizeObserver: ResizeObserver | null = null;
  // Scroll-anchoring / follow-hardening / selection state, mirrored from ChatPanel
  // (see there for the rationale). The subagent transcript has no phantom paging or
  // eviction, so this is the simpler half: anchor while scrolled up, tail-pin
  // otherwise, defer the pin mid-drag, and keep selected rows mounted.
  let scrollAnchor: ScrollAnchor | null = null;
  let lastScrollHeight = 0;
  let isPointerDown = false;
  let pointerReleaseListener: (() => void) | null = null;
  // Memoized rows. buildConversationRows walks the whole subagent transcript, so
  // it is recomputed only when the event set or idleness changes -- not on every
  // scroll redraw. The transcript is append-only here (no in-place upgrades, no
  // eviction), so the event count plus the idle flag is a sufficient cache key.
  let rowsCacheKey = "";
  let cachedRows: RowDescriptor[] = [];
  // Row key -> index in cachedRows, for resolving a selection's DOM rows to pin.
  let cachedKeyToIndex = new Map<string, number>();

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

  function applyScrollPosition(element: HTMLElement): void {
    if (userScrolledUp) {
      applyScrollAnchor(element);
    } else {
      applyTailFollow(element);
    }
    lastScrollHeight = element.scrollHeight;
  }

  // Hold the anchored row fixed against height changes above it (relative delta so
  // in-flight user scrolling is preserved). See ChatPanel.applyScrollAnchor.
  function applyScrollAnchor(element: HTMLElement): void {
    if (scrollAnchor === null) {
      return;
    }
    const currentTop = contentTopOfRow(element, scrollAnchor.key);
    if (currentTop === null) {
      scrollAnchor = null;
      return;
    }
    const delta = currentTop - scrollAnchor.contentTop;
    if (delta !== 0) {
      element.scrollTop = element.scrollTop + delta;
    }
    scrollAnchor = { key: scrollAnchor.key, contentTop: currentTop };
    scrollTop = element.scrollTop;
    previousScrollTop = element.scrollTop;
  }

  function applyTailFollow(element: HTMLElement): void {
    if (isPointerDown) {
      return;
    }
    const maxScroll = element.scrollHeight - element.clientHeight;
    if (element.scrollTop < Math.min(scrollTop, maxScroll) - 1) {
      userScrolledUp = true;
      scrollTop = element.scrollTop;
      previousScrollTop = element.scrollTop;
      scrollAnchor = null;
      return;
    }
    element.scrollTop = element.scrollHeight;
    scrollTop = element.scrollTop;
    previousScrollTop = element.scrollTop;
  }

  function handleScrollEvent(event: Event): void {
    const element = event.target as HTMLElement;
    const didScrollUp = element.scrollTop < previousScrollTop;
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 40;
    // A shrink-clamp looks like a scroll-up but carries no user intent; preserve
    // the follow state rather than re-deriving it (see scrollFollow).
    const isClamp = didScrollUp && element.scrollHeight < lastScrollHeight && atBottom;
    previousScrollTop = element.scrollTop;
    scrollTop = element.scrollTop;
    // A subagent transcript is a single loaded list with no off-tail jump, so
    // there is never newer unloaded history below: hasMoreAfter is always false.
    userScrolledUp = nextUserScrolledUp({
      didScrollUp,
      isNearBottom: atBottom,
      hasMoreAfter: false,
      isClamp,
      wasUserScrolledUp: userScrolledUp,
    });
    scrollAnchor = userScrolledUp ? captureTopAnchor(element) : null;
    lastScrollHeight = element.scrollHeight;
  }

  // Refresh the cached viewport height and schedule a measure pass; the
  // measure/cache mechanics live in the shared row measurer.
  function scheduleMeasure(): void {
    if (scrollEl !== null) {
      viewportHeight = scrollEl.clientHeight;
    }
    rowMeasurer.scheduleMeasure(() => scrollEl);
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
      rowMeasurer.prune(new Set(cachedRows.map((row) => row.key)));
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;
    const getHeight = (index: number): number => rowMeasurer.getHeight(rows[index].key) ?? rows[index].estimate;
    const effectiveViewportHeight = viewportHeight > 0 ? viewportHeight : (scrollEl?.clientHeight ?? 2000);
    const baseWindow = computeVisibleWindow({
      count: rows.length,
      getHeight,
      scrollTop,
      viewportHeight: effectiveViewportHeight,
      overscanPx: OVERSCAN_PX,
    });
    // Keep the rows holding a live selection mounted so scrolling/streaming past
    // them doesn't collapse the selection; drop the pin past the gap cap.
    let pinnedRange = resolveSelectionRowRange(scrollEl, cachedKeyToIndex);
    if (pinnedRange !== null) {
      const gapAbove = baseWindow.startIndex - pinnedRange.end;
      const gapBelow = pinnedRange.start - baseWindow.endIndex;
      if (gapAbove > SELECTION_PIN_MAX_GAP_ROWS || gapBelow > SELECTION_PIN_MAX_GAP_ROWS) {
        pinnedRange = null;
      }
    }
    // Keep the scroll-anchor row mounted while scrolled up so applyScrollAnchor can
    // always measure it (see ChatPanel.withAnchorPinned).
    if (userScrolledUp && scrollAnchor !== null) {
      const anchorIndex = cachedKeyToIndex.get(scrollAnchor.key);
      if (anchorIndex !== undefined) {
        pinnedRange =
          pinnedRange === null
            ? { start: anchorIndex, end: anchorIndex }
            : { start: Math.min(pinnedRange.start, anchorIndex), end: Math.max(pinnedRange.end, anchorIndex) };
      }
    }
    const windowResult =
      pinnedRange === null
        ? baseWindow
        : computeVisibleWindow({
            count: rows.length,
            getHeight,
            scrollTop,
            viewportHeight: effectiveViewportHeight,
            overscanPx: OVERSCAN_PX,
            pinnedRange,
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
      if (pointerReleaseListener !== null) {
        window.removeEventListener("pointerup", pointerReleaseListener);
        window.removeEventListener("pointercancel", pointerReleaseListener);
        pointerReleaseListener = null;
      }
      scrollEl = null;
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
            onscroll: handleScrollEvent,
            onpointerdown: () => {
              isPointerDown = true;
            },
            oncreate: (mainVnode: m.VnodeDOM) => {
              scrollEl = mainVnode.dom as HTMLElement;
              viewportHeight = scrollEl.clientHeight;
              pointerReleaseListener = () => {
                if (isPointerDown) {
                  isPointerDown = false;
                  m.redraw();
                }
              };
              window.addEventListener("pointerup", pointerReleaseListener);
              window.addEventListener("pointercancel", pointerReleaseListener);
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
