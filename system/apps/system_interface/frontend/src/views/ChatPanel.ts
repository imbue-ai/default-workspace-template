/**
 * Chat panel for dockview. Contains the main message list and message input
 * for an agent, mounted as a tab within the dockview workspace.
 *
 * If the agent is still being created (a proto-agent), shows the creation
 * log stream instead. Automatically switches to the chat view when creation
 * completes.
 */

import m from "mithril";
import { isSlotClaimed } from "../slots";
import {
  fetchBackfillEvents,
  fetchForwardEvents,
  fetchWindowAtOffset,
  getEventsForAgent,
  getEventCount,
  getFirstOffset,
  getRenderVersion,
  getTotalEventCount,
  evictOldEvents,
  hasMoreBefore,
  hasMoreAfter,
  isConversationNotFound,
  MAX_HELD_EVENTS,
} from "../models/Response";
import { isSelectionActiveWithin } from "../models/scrollFollow";
import {
  DEFAULT_EVENT_HEIGHT_PX,
  RowGeometryIndex,
  geometryFromSnapshot,
  type GeometrySnapshot,
} from "../models/rowGeometry";
import { sharedGeometryCache, widthBucketFor } from "../models/geometryCache";
import { loadWorkspaceGeometry, saveWorkspaceGeometry } from "../models/workspaceGeometry";
import { resolveSelectionRowIndices, selectionStateWithin } from "./scroll-selection";
import { createTranscriptScroll } from "./transcript-scroll";
import { createTranscriptVirtualizer } from "./transcriptVirtualizer";
import { createRowMeasureScheduler, createRowMeasurementStore } from "./rowMeasurement";
import { connectToStream, disconnectFromStream, loadSnapshotWithStream } from "../models/StreamingMessage";
import {
  addAgentsUpdatedListener,
  getAgentById,
  getProtoAgents,
  removeAgentsUpdatedListener,
} from "../models/AgentManager";
import { openAgentAuth } from "../models/AgentAuth";
import { maybePromptForFastMode } from "./fast-mode-prompt";
import { apiUrl } from "../base-path";
import { EmptySlot } from "./EmptySlot";
import { uploadFilesToComposer } from "../models/ComposerAttachments";
import { MessageInput } from "./MessageInput";
import { PoweredByCredit } from "./PoweredByCredit";
import { ModelBar } from "./ModelBar";
import { buildAgentTerminalUrl, getTerminalUrl, openIframeTabForAgent } from "./DockviewWorkspace";
import { buildConversationRows, renderVirtualRows, type RowDescriptor } from "./conversation-rows";
import { ActivityIndicator } from "./ActivityIndicator";
import { renderQueuedMessages } from "./QueuedMessageView";
import { renderOutgoingMessages } from "./OutgoingMessageView";

function getAgentTerminalUrl(agentId: string): string {
  // The ttyd dispatch script is invoked as `bash -c "$SCRIPT" <args...>` where
  // the first trailing arg becomes $0 (not $1). ``buildAgentTerminalUrl``
  // emits ``arg=_&arg=agent&arg=<name>`` so the dispatch lands ``agent`` in
  // ``$1`` and the name in ``$2``, mirroring the workdir deep-link pattern.
  // When the agent isn't in the local cache yet, fall back to the bare
  // base URL and let agent.sh attach to the ambient session.
  const agent = getAgentById(agentId);
  if (!agent?.name) {
    const baseUrl = getTerminalUrl();
    const separator = baseUrl.includes("?") ? "&" : "?";
    return `${baseUrl}${separator}arg=_&arg=agent`;
  }
  return buildAgentTerminalUrl(agent.name);
}

function openAgentTerminalTab(agentId: string): void {
  const agent = getAgentById(agentId);
  const title = agent?.name ? `${agent.name} terminal` : "agent terminal";
  openIframeTabForAgent(agentId, getAgentTerminalUrl(agentId), title);
}

// Layout for the centered message column. Shared between the normal transcript
// render and the empty-state branch that shows an optimistic first message, so
// the two stay visually identical.
const MESSAGE_LIST_CLASS = "message-list mx-auto w-full max-w-(--width-message-column) flex flex-col py-6";
// Backfill fires when the viewport is within this many pixels of the top or
// bottom edge of the loaded rows (and the server reports more history there).
const BACKFILL_TRIGGER_PX = 600;
// When the scroll position maps to an event more than this many events beyond the
// loaded window, jump (replace the window around the target) instead of paging
// there incrementally. Small enough that ordinary scrolling keeps paging; large
// enough that a couple of pages' overshoot doesn't trigger a disruptive reload.
const JUMP_GAP_EVENTS = 120;
// Coalescing window for persisting geometry, so a burst of rows settling after a
// paint costs one write rather than one per row.
const GEOMETRY_SAVE_DEBOUNCE_MS = 2000;
// How far the observed per-event rate must move, relative to the rate in use,
// before the reserve adopts it. Stops the reserved height churning once it has
// converged, while still letting the cold default be replaced by reality.
const RESERVE_RATE_CHANGE_THRESHOLD = 0.1;
// How many measured rows are enough to fix the reserve rate for good. The reserve
// prices history this client has never seen, so being approximately right and
// STABLE beats being precise and moving: every later refinement would resize the
// scroll container under a reader, and a shrink that pulls the bottom up to the
// viewport re-arms tail-following underneath them. Twenty rows is a
// representative sample of a transcript's mix, and is reached within the first
// screens.
const RESERVE_RATE_SAMPLE_ROWS = 20;

function isProtoAgent(agentId: string): boolean {
  return getProtoAgents().some((p) => p.agent_id === agentId);
}

export function ChatPanel(): m.Component<{ agentId: string; isVisible?: boolean }> {
  let loading = false;
  let loadingError: string | null = null;
  let currentAgentId: string | null = null;

  // Whether this panel is the visible (selected) tab in its dockview group.
  // dockview keeps an inactive tab mounted (defaultRenderer: "always") and
  // mithril redraws globally, so the component keeps running while hidden
  // against an element collapsed to zero size; running scroll work then would
  // corrupt the retained scroll position. The renderer feeds dockview's
  // authoritative visibility in via the ``isVisible`` attr (see
  // createMithrilRenderer); the scroll hooks below skip while it is false.
  // Defaults to true so the panel works before the first render sets it.
  let panelVisible = true;
  // Shared scroll controller: owns the scroll position, follow state, drag flag
  // and the tail-follow / native-anchoring / pointer / resize machinery. Windowing
  // is the virtualizer's job; this remains the single owner of the follow decision,
  // which is the part every previous attempt at this got wrong.
  const scroll = createTranscriptScroll({
    isVisible: () => panelVisible,
    getHasMoreAfter: () => hasMoreAfter(currentAgentId ?? ""),
    onUserScroll: (element) => {
      if (currentAgentId !== null) {
        maybePage(currentAgentId, element);
      }
    },
  });
  // Memoized turn-grouping output. buildSections walks the whole held
  // transcript, so it is recomputed only when the data actually changes (keyed
  // on the render version + idle flag), not on every scroll-driven redraw.
  let rowsCacheKey: string | null = null;
  let cachedRows: RowDescriptor[] = [];
  // Row key -> index in cachedRows, memoized alongside it. Used to resolve a live
  // selection's DOM rows to virtualization indices so they can be pinned into the
  // window (see renderMessages).
  let cachedKeyToIndex = new Map<string, number>();
  // Accepted row heights, with the hysteresis and settle tracking that keep a
  // sub-pixel reflow from looping and a pre-markdown placeholder from being
  // remembered as final.
  const measurements = createRowMeasurementStore();
  // Measured geometry for this conversation: which rows cover which events and
  // how tall each is. Reserved space above and below the loaded window is read
  // straight out of this, so it reflects what the transcript actually renders.
  let geometry = new RowGeometryIndex();
  // Heights reserved above/below the loaded window for history that exists on the
  // server but isn't loaded yet. Shared so the scroll handler can tell when the
  // viewport is over a reserved region and page/jump/overlay accordingly.
  let phantomTopHeight = 0;
  let phantomBottomHeight = 0;
  // The row the reader was on last frame, and where in the viewport it sat, so
  // it can be put back after the window is recomputed (see restoreReadingAnchor).
  let anchorRowKey: string | null = null;
  let anchorViewportOffset = 0;
  // Whether the panel was visible on the previous render, so becoming visible
  // again can be treated as a resume rather than as a shift to correct.
  let wasPanelVisible = true;
  // Which width bucket the held geometry describes; -1 until the first measure,
  // so the first real width always counts as a change and triggers a load.
  let geometryWidthBucket = -1;
  let geometrySaveTimer: ReturnType<typeof setTimeout> | null = null;
  // Windowing. Reads the memoized rows and the reserved heights above; owns the
  // visible range, the selection pin and scroll compensation.
  const virtualizer = createTranscriptVirtualizer({
    getScrollElement: () => scroll.scrollEl,
    getCount: () => cachedRows.length,
    getRowKey: (index) => cachedRows[index]?.key ?? String(index),
    estimateSize: (index) => {
      const row = cachedRows[index];
      if (row === undefined) {
        return 0;
      }
      const measured = measurements.heightFor(row.key);
      if (measured !== undefined) {
        return measured;
      }
      // Estimate an unmeasured row from what this transcript actually renders
      // at, not from a per-kind constant. The constants are necessarily generic
      // -- an assistant row is 240px whether it holds three words or a screenful
      // -- and on a transcript of short messages every newly rendered row then
      // shrank by a couple of hundred pixels the moment it was measured.
      // Scrolling up rendered a screenful of them at once, the content collapsed
      // by thousands of pixels, and the bottom rose to meet a reader who had
      // deliberately scrolled away from it -- which re-armed tail-following and
      // dragged them back down.
      const events = row.end_offset - row.start_offset;
      if (isReserveRateSettled && events > 0) {
        return Math.max(1, reserveRate * events);
      }
      return row.estimate;
    },
    getPaddingStart: () => phantomTopHeight,
    getPaddingEnd: () => phantomBottomHeight,
    getPinnedIndices: () => pinnedRowIndices,
    isEnabled: () => panelVisible,
  });
  // Row indices a live text selection touches, recomputed each render and read
  // by the virtualizer's range extractor.
  let pinnedRowIndices: number[] = [];
  // The frame-debounced measure pass. A hidden panel has nothing laid out, so it
  // reports no list and the frame reads nothing.
  const measureScheduler = createRowMeasureScheduler({
    store: measurements,
    getListElement: () => (panelVisible ? (scroll.scrollEl?.querySelector(".message-list") ?? null) : null),
    reportHeight: (rowKey, height) => {
      const index = cachedKeyToIndex.get(rowKey);
      if (index !== undefined) {
        virtualizer.resizeRow(index, height);
      }
    },
  });
  // Paging (scroll-driven fetch) in-flight guard. Covers older/newer pages and
  // offset jumps -- only one is outstanding at a time.
  let backfillInFlight = false;
  // After an offset jump replaces the window, pin the viewport once to the top of
  // the freshly loaded rows (just below the top reserved spacer) so the user lands
  // on the jumped-to content rather than in the reserved region above it. A single
  // pin suffices -- no timed settle -- because the space above those rows is priced
  // off geometry that has already settled and a per-event rate that has stopped
  // moving (see updateReserveRate), so it does not drift as the new rows measure.
  let pendingPinToWindowTop = false;

  // File drag-and-drop: dropping a file anywhere over the chat stages it as a
  // composer attachment. ``dragDepth`` counts dragenter minus dragleave across
  // nested children so the overlay does not flicker as the cursor moves between
  // transcript rows; the overlay is shown while the depth is positive.
  let dragDepth = 0;
  let isFileDragActive = false;

  function isFileDrag(event: DragEvent): boolean {
    const types = event.dataTransfer?.types;
    return types !== undefined && Array.from(types).includes("Files");
  }

  function handleDragEnter(event: DragEvent): void {
    if (!isFileDrag(event)) {
      return;
    }
    event.preventDefault();
    dragDepth = dragDepth + 1;
    if (!isFileDragActive) {
      isFileDragActive = true;
      m.redraw();
    }
  }

  function handleDragOver(event: DragEvent): void {
    if (!isFileDrag(event)) {
      return;
    }
    // Required so the element is a valid drop target (the browser otherwise
    // rejects the drop).
    event.preventDefault();
  }

  function handleDragLeave(event: DragEvent): void {
    if (!isFileDrag(event) || dragDepth === 0) {
      return;
    }
    dragDepth = dragDepth - 1;
    if (dragDepth === 0 && isFileDragActive) {
      isFileDragActive = false;
      m.redraw();
    }
  }

  function handleDrop(event: DragEvent, agentId: string): void {
    dragDepth = 0;
    const wasActive = isFileDragActive;
    isFileDragActive = false;
    if (!isFileDrag(event)) {
      if (wasActive) {
        m.redraw();
      }
      return;
    }
    event.preventDefault();
    uploadFilesToComposer(agentId, event.dataTransfer?.files);
    m.redraw();
  }

  // Snapshot-load path: SSE only carries events emitted after subscription,
  // so an auth-error that happened before the user opened the panel (e.g.
  // the auto-`/welcome` failing during fresh mind creation) wouldn't open
  // the modal otherwise. Walking back to the last assistant_message means
  // an already-recovered agent (whose history contains old auth errors
  // but has since produced healthy replies) does not open it on reload --
  // only an agent whose current state is broken does. The modal itself is
  // a single app-level instance driven by global auth state (see
  // models/ClaudeAuth.ts), so this just flips that shared flag.
  function checkLatestAssistantForAuthError(agentId: string): void {
    const events = getEventsForAgent(agentId);
    for (let i = events.length - 1; i >= 0; i--) {
      const event = events[i];
      if (event.type === "assistant_message") {
        if (event.is_auth_error === true) {
          openAgentAuth(agentId);
        }
        return;
      }
    }
  }

  // Screen capture state (shown when agent has no conversation)
  let screenContent: string | null = null;
  let screenError: string | null = null;
  let screenLoading = false;
  // The agent a capture has already been attempted for. Set before the request
  // and never cleared for that agent, so an attempt that comes back empty (a
  // crashed agent with no pane to capture, or a 404 while the agent is still
  // being registered) does not re-arm the fetch. The not-found view calls this
  // from every render and the fetch ends in `m.redraw()`, so a guard keyed on
  // the *result* -- as an unset `screenContent` was -- feeds itself: each empty
  // result triggers the redraw that issues the next request, which is an
  // unbounded request loop rather than the one-shot capture the view wants.
  let screenAttemptedAgentId: string | null = null;

  // Proto-agent log state
  let logWs: WebSocket | null = null;
  let logLines: string[] = [];
  let logDone = false;
  let logSuccess = false;
  let logError: string | null = null;
  let logAgentId: string | null = null;

  async function fetchScreenCapture(agentId: string): Promise<void> {
    if (screenAttemptedAgentId === agentId) {
      return;
    }
    screenAttemptedAgentId = agentId;
    screenLoading = true;
    screenContent = null;
    screenError = null;
    try {
      const result = await m.request<{ screen: string | null; error?: string }>({
        method: "GET",
        url: apiUrl("/api/agents/:agentId/screen"),
        params: { agentId, scrollback: "true" },
      });
      screenContent = result.screen;
      screenError = result.error ?? null;
    } catch {
      screenError = "Failed to capture screen";
    } finally {
      screenLoading = false;
      m.redraw();
    }
  }

  function connectLogWs(agentId: string): void {
    if (logWs !== null) {
      logWs.close();
    }
    logLines = [];
    logDone = false;
    logSuccess = false;
    logError = null;
    logAgentId = agentId;

    const base = apiUrl(`/api/proto-agents/${encodeURIComponent(agentId)}/logs`);
    const loc = window.location;
    const protocol = loc.protocol === "https:" ? "wss:" : "ws:";
    let url: string;
    if (base.startsWith("http")) {
      url = base.replace(/^http/, "ws");
    } else {
      url = `${protocol}//${loc.host}${base}`;
    }

    logWs = new WebSocket(url);

    logWs.onmessage = (event: MessageEvent) => {
      const data = JSON.parse(event.data as string) as
        | { line: string }
        | { done: true; success: boolean; error: string | null };

      if ("line" in data) {
        logLines.push(data.line);
      } else if ("done" in data) {
        logDone = true;
        logSuccess = data.success;
        logError = data.error;
      }
      m.redraw();
    };

    logWs.onclose = () => {
      logWs = null;
    };

    logWs.onerror = () => {
      logWs?.close();
    };
  }

  function disconnectLogWs(): void {
    if (logWs !== null) {
      logWs.close();
      logWs = null;
    }
    logAgentId = null;
  }

  function renderBuildLog(agentId: string): m.Vnode {
    if (logAgentId !== agentId) {
      connectLogWs(agentId);
    }

    return m("div", { style: "display: flex; flex-direction: column; height: 100%; padding: 16px;" }, [
      m(
        "div",
        { style: "font-weight: 600; margin-bottom: 8px; font-size: 0.9em; color: #666;" },
        logDone ? (logSuccess ? "Agent created successfully" : "Agent creation failed") : "Creating agent...",
      ),
      logError ? m("div", { style: "color: red; margin-bottom: 8px; font-size: 0.85em;" }, logError) : null,
      m(
        "div",
        {
          style:
            "flex: 1; overflow-y: auto; background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 0.8em; padding: 12px; border-radius: 4px; white-space: pre-wrap; word-break: break-all;",
          onupdate(vnode: m.VnodeDOM) {
            const el = vnode.dom as HTMLElement;
            el.scrollTop = el.scrollHeight;
          },
        },
        logLines.map((line, i) => m("div", { key: i, style: "line-height: 1.5;" }, line)),
      ),
    ]);
  }

  async function loadAgent(agentId: string): Promise<void> {
    loading = true;
    loadingError = null;

    try {
      // Buffer SSE deltas arriving during the snapshot fetch so the wholesale
      // snapshot replace in fetchEvents cannot drop a live event on first load.
      await loadSnapshotWithStream(agentId);
      if (agentId === currentAgentId) {
        loading = false;
        loadingError = null;
        checkLatestAssistantForAuthError(agentId);
      }
    } catch (error) {
      if (agentId === currentAgentId) {
        loading = false;
        // mithril attaches the parsed JSON error body to `.response`; the server
        // sends the human-readable reason there as `detail`. Reading `.message`
        // alone surfaces the raw body object as "[object Object]".
        const errResp = (error as { response?: { detail?: string } }).response;
        loadingError = errResp?.detail ?? (error as Error).message ?? String(error);
      }
    }
  }

  function manageStreamConnection(agentId: string): void {
    if (!isConversationNotFound(agentId)) {
      connectToStream(agentId);
    } else {
      disconnectFromStream(agentId);
    }
  }

  function ensureAgentLoaded(agentId: string): void {
    if (agentId === currentAgentId) {
      return;
    }

    currentAgentId = agentId;
    scroll.reset();
    // A different conversation's rows share no keys with this one's, and its
    // geometry is indexed on its own transcript offsets, so both are dropped
    // rather than carried across.
    measurements.reset();
    cancelGeometrySave();
    geometry = new RowGeometryIndex();
    geometryWidthBucket = -1;
    reserveRate = DEFAULT_EVENT_HEIGHT_PX;
    isReserveRateSettled = false;
    virtualizer.reset();
    rowsCacheKey = null;
    backfillInFlight = false;
    loadAgent(agentId);
  }

  // A retry of the snapshot that 404'd is outstanding; only one at a time.
  let notFoundRetryInFlight = false;

  /**
   * Re-load a panel whose first events fetch 404'd, once the backend knows the
   * agent.
   *
   * A newly created chat lands in that window by construction: create-chat
   * returns 201 as soon as the background `mngr create` starts, and the agent is
   * only registered when that finishes, so the panel's first fetch races ahead
   * of it. `fetchEvents` latches the miss and only ever clears it on its own next
   * call, which `ensureAgentLoaded` never makes for an agent it has already
   * loaded -- so without this the panel sits on "No conversation data" until the
   * page is reloaded.
   *
   * The trigger is the `agents_updated` snapshot rather than a retry timer, and
   * it cannot spin: `/events` resolves the agent through the same
   * `AgentManager._agents` that feeds `agents_updated`, so the agent being named
   * here is exactly the condition under which the refetch stops 404ing.
   */
  function retryAfterAgentResolved(): void {
    const agentId = currentAgentId;
    if (agentId === null || notFoundRetryInFlight || !isConversationNotFound(agentId)) {
      return;
    }
    // Read the agent store rather than the broadcast payload, which is filtered
    // to the user-facing agents.
    if (getAgentById(agentId) === undefined) {
      return;
    }
    notFoundRetryInFlight = true;
    loadAgent(agentId).finally(() => {
      notFoundRetryInFlight = false;
      m.redraw();
    });
  }

  /**
   * Keep the loaded window in step with the scroll position. Three cases, all
   * bounded to a single fetch:
   *   - viewport far from the loaded window (e.g. a scrollbar drag deep into
   *     history): JUMP -- replace the window with a page around the target offset,
   *     so reaching a distant point costs one request, not a walk through
   *     everything between.
   *   - viewport near the top edge of the loaded rows, with older history left:
   *     page one older window-worth.
   *   - viewport near the bottom edge, with newer history left (only possible
   *     after a jump moved the window off the live tail): page one newer worth.
   */
  function maybePage(agentId: string, element: HTMLElement): void {
    // While the panel is hidden (an inactive dockview tab) the element is
    // zero-sized: scrollTop/scrollHeight read 0, which would map the viewport to
    // event 0 and fire a spurious jump to the start of the conversation. Skip.
    if (!panelVisible) {
      return;
    }
    // A fetch is already outstanding (only one at a time), or a just-completed jump
    // still needs its one-shot pin applied -- in both cases the window is about to
    // change, so don't act on the current (transient) scroll position.
    if (backfillInFlight || pendingPinToWindowTop) {
      return;
    }
    const held = getEventCount(agentId);
    const firstOffset = getFirstOffset(agentId);
    const windowEnd = firstOffset + held;

    // Map the viewport to a target event index using the SAME phantom-region
    // geometry the renderer uses to size the reserved spacers, so it is the exact
    // inverse. Only the reserved regions above/below the loaded window can imply a
    // jump; over the loaded rows the edge-paging branches below handle it. Deriving
    // the target from the scroll height as a whole instead would drift as the loaded
    // rows measure, and could cross the jump threshold on its own -- firing a window
    // reset nobody asked for, which unmounts every row: the most violent scroll jolt
    // there is, and a guaranteed selection kill.
    const total = getTotalEventCount(agentId);
    const loadedBottom = element.scrollHeight - phantomBottomHeight;
    let targetIndex: number | null = null;
    if (phantomTopHeight > 0 && element.scrollTop < phantomTopHeight) {
      // The exact inverse of the function that sized the reserved space, so the
      // position the thumb is at and the offset it resolves to cannot disagree.
      targetIndex = geometry.offsetAtHeight(element.scrollTop, firstOffset, reserveRate);
    } else if (phantomBottomHeight > 0 && element.scrollTop + element.clientHeight > loadedBottom) {
      const intoBottomRegion = element.scrollTop + element.clientHeight - loadedBottom;
      targetIndex = geometry.offsetAtHeight(
        geometry.heightBefore(windowEnd, reserveRate) + intoBottomRegion,
        total,
        reserveRate,
      );
    }

    // Far from the loaded window in either direction -> jump.
    if (
      targetIndex !== null &&
      (targetIndex < firstOffset - JUMP_GAP_EVENTS || targetIndex > windowEnd + JUMP_GAP_EVENTS)
    ) {
      backfillInFlight = true;
      fetchWindowAtOffset(agentId, targetIndex - Math.floor(JUMP_GAP_EVENTS / 2)).finally(() => {
        backfillInFlight = false;
        // The window now sits off the live tail, so stop following it, and pin the
        // viewport once to the new window's top on the next redraw (applyScrollPosition).
        scroll.userScrolledUp = true;
        pendingPinToWindowTop = true;
        m.redraw();
      });
      return;
    }

    // Near the top of the loaded rows -> page older. Native scroll anchoring keeps
    // the viewport fixed on the content being read when the older page lands above.
    if (hasMoreBefore(agentId) && element.scrollTop - phantomTopHeight < BACKFILL_TRIGGER_PX) {
      backfillInFlight = true;
      fetchBackfillEvents(agentId).finally(() => {
        backfillInFlight = false;
        m.redraw();
      });
      return;
    }

    // Near the bottom of the loaded rows with newer history left -> page newer.
    // Appending below shifts nothing above it, so no scroll compensation is due.
    const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
    if (hasMoreAfter(agentId) && distanceFromBottom - phantomBottomHeight < BACKFILL_TRIGGER_PX) {
      backfillInFlight = true;
      fetchForwardEvents(agentId).finally(() => {
        backfillInFlight = false;
        m.redraw();
      });
    }
  }

  /**
   * Note which row the reader is looking at, and where in the viewport it sits.
   *
   * Captured before the window is recomputed, in the virtualizer's own offset
   * space rather than by measuring the DOM. That distinction is the whole point:
   * a DOM-measured anchor is sampled against rows that are still settling, so it
   * oscillates and rectifies into drift. These offsets are the same numbers the
   * layout is about to be built from, so restoring against them is exact.
   */
  function captureReadingAnchor(): void {
    const justBecameVisible = panelVisible && !wasPanelVisible;
    wasPanelVisible = panelVisible;
    anchorRowKey = null;
    // Nothing was being held while hidden -- the window was frozen against a
    // zero-sized element -- so there is no position to restore, only one to
    // adopt. The browser preserved the real scrollTop across hide/show, so that
    // is the truth; correcting against offsets computed before the freeze would
    // move the reader instead of holding them. Anchoring resumes next frame,
    // once the window has been recomputed against the live viewport.
    if (justBecameVisible || !panelVisible || !scroll.userScrolledUp) {
      return;
    }
    const top = scroll.scrollEl?.scrollTop ?? scroll.scrollTop;
    for (const item of virtualizer.getVirtualItems()) {
      if (item.end > top) {
        anchorRowKey = String(item.key);
        anchorViewportOffset = item.start - top;
        return;
      }
    }
  }

  /**
   * Put the reader back on the row they were reading, at the same place in the
   * viewport.
   *
   * Everything above that row can legitimately change between two frames: the
   * reserved estimate for unloaded history is refined as rows are measured, and
   * a backfilled page replaces reserved space with real rows. Those two move in
   * opposite directions and very nearly cancel, which is why correcting for
   * either alone is wrong: compensating for the reserved-space change by itself
   * walks the reader to the top of the conversation, because the rows that
   * landed have already made up the difference. Holding the anchor's position is
   * the invariant that actually matters, and it subsumes both.
   *
   * This is also why the virtualizer's own scroll compensation is switched off:
   * two mechanisms writing scrollTop for overlapping reasons double-correct, and
   * this one is strictly more general.
   *
   * Skipped while following the tail, where the tail pin owns the position
   * outright, and while hidden, where the element is zero-sized.
   */
  function restoreReadingAnchor(element: HTMLElement): void {
    if (anchorRowKey === null || !panelVisible || !scroll.userScrolledUp) {
      return;
    }
    for (const item of virtualizer.getVirtualItems()) {
      if (String(item.key) !== anchorRowKey) {
        continue;
      }
      const target = Math.max(0, item.start - anchorViewportOffset);
      // A sub-pixel difference is layout rounding, not drift; writing scrollTop
      // for it would feed the loop it is meant to prevent.
      if (Math.abs(target - element.scrollTop) > 1) {
        scroll.pinTo(element, target);
      }
      return;
    }
  }

  function applyScrollPosition(element: HTMLElement): void {
    // Hidden panels and the tail-follow pin are handled by the shared controller;
    // this wrapper only adds the offset-jump pin that is specific to the main chat.
    if (!panelVisible) {
      return;
    }
    // After an offset jump, pin the viewport once to the top of the freshly loaded
    // rows (just below the top reserved spacer) so the user lands on the jumped-to
    // content rather than in the reserved (blank) region above it. The reserved top
    // height covers history that is not loaded, priced off settled geometry and a
    // rate that has stopped moving, so it doesn't drift as the loaded rows measure
    // -- a single pin lands correctly without a timed settle.
    if (pendingPinToWindowTop) {
      pendingPinToWindowTop = false;
      scroll.pinTo(element, phantomTopHeight);
      return;
    }
    scroll.applyScrollPosition(element);
  }

  function renderMessages(agentId: string): m.Vnode {
    // Reset here so the loading overlay (keyed on a positive value) stays hidden
    // for every path that doesn't render the windowed list; the windowed path
    // below sets the real reserved heights.
    phantomTopHeight = 0;
    phantomBottomHeight = 0;

    // The build log covers creation, so it only applies while the agent is not
    // yet a real one. Both branches below are gated on that: the proto-agent
    // list is rebuilt from broadcasts and can name an agent that has since been
    // registered (the `proto_agent_created` for a finished creation, delivered
    // late), and asking for its creation log then gets the backend's
    // "Proto-agent not found" -- which reads as `logDone && !logSuccess` and
    // would strand a perfectly healthy chat on a "creation failed" screen.
    const isRegisteredAgent = getAgentById(agentId) !== undefined;

    // If this agent is still being created, show the build log
    if (isProtoAgent(agentId) && !isRegisteredAgent) {
      return renderBuildLog(agentId);
    }

    // Creation completed but failed -- keep the build log visible so the
    // user can read the error and the last few log lines. Without this the
    // build-log view transitions to the empty-chat / "no conversation data"
    // screen the instant proto_agent_completed arrives and the error flashes
    // by unreadably. The agent will never be added to getAgents() on
    // failure, so nothing else in the UI would surface the error either.
    if (logAgentId === agentId && logDone && !logSuccess && !isRegisteredAgent) {
      return renderBuildLog(agentId);
    }

    // Agent finished creating successfully -- disconnect log WebSocket and
    // force reload
    if (logAgentId === agentId) {
      disconnectLogWs();
      currentAgentId = null;
    }

    ensureAgentLoaded(agentId);
    manageStreamConnection(agentId);

    if (isConversationNotFound(agentId)) {
      fetchScreenCapture(agentId);
      return m("div", { class: "message-list-not-found flex flex-col items-center justify-center h-full gap-4 p-8" }, [
        m("p", { class: "text-lg font-semibold text-text-primary" }, "No conversation data"),
        m("p", { class: "text-text-secondary" }, "This agent has no Claude session. It may have crashed on startup."),
        screenLoading
          ? m("p", { class: "text-text-secondary" }, "Loading terminal output...")
          : screenContent
            ? m(
                "pre",
                {
                  class:
                    "text-sm bg-gray-900 text-gray-100 p-4 rounded-lg overflow-auto w-full max-h-96 font-mono whitespace-pre",
                },
                screenContent,
              )
            : screenError
              ? m("p", { class: "text-text-secondary text-sm" }, `Could not capture terminal: ${screenError}`)
              : null,
      ]);
    }

    if (loading) {
      return m(
        "div",
        { class: "message-list-loading flex items-center justify-center h-full" },
        m("p", { class: "text-text-secondary" }, "Loading events..."),
      );
    }

    if (loadingError) {
      return m(
        "div",
        { class: "message-list-error flex items-center justify-center h-full" },
        m("p", { class: "text-red-500" }, `Error: ${loadingError}`),
      );
    }

    // Whether a live text selection is anchored in this panel's transcript. Gates
    // both eviction (below) and the tail-follow pin's effect on the window (via the
    // selection pin further down): a selection must survive scrolling and streaming.
    const selectionActive = isSelectionActiveWithin(selectionStateWithin(scroll.scrollEl));

    // Bound client memory while following the live tail: trim the oldest held
    // events once well over the cap. Only when at the bottom, so a scrolled-up
    // reader's rendered history is never yanked out from under them; the dropped
    // history is re-fetched via backfill on scroll-up (evictOldEvents advances the
    // window start so it reads as older history above). Re-pinned to the bottom by
    // applyScrollPosition afterwards. Also skipped while a selection is active:
    // eviction deletes the underlying events, which no amount of DOM pinning can
    // survive. This temporarily lifts the MAX_HELD_EVENTS bound while a selection
    // is held; it is restored on the first redraw after the selection is dropped.
    if (!scroll.userScrolledUp && !selectionActive && getEventCount(agentId) > MAX_HELD_EVENTS) {
      evictOldEvents(agentId);
    }

    const events = getEventsForAgent(agentId);

    if (events.length === 0) {
      // No transcript yet -- but a message the user just sent may already be
      // queued (the harness parked it) or still in flight (an optimistic
      // "Sending…" bubble), so render those rather than the empty-state placeholder.
      const tailNodes = [...renderQueuedMessages(agentId), ...renderOutgoingMessages(agentId)];
      if (tailNodes.length === 0) {
        return m(
          "div",
          { class: "message-list-empty flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "No events yet for this agent."),
        );
      }
      return m("div", { class: "message-list-wrapper" }, [m("div", { class: MESSAGE_LIST_CLASS }, tailNodes)]);
    }

    const agent = getAgentById(agentId);
    const agentIsIdle = agent?.activity_state === "IDLE";

    // The first chat starts on fast mode; once it has run its grace period, ask
    // the user whether to keep it. Checked here because this is where the loaded
    // transcript and the idle flag meet. Re-running it per render is fine:
    // raising the prompt is idempotent, and the cheap gates (harness declared no
    // prompt, not the first chat, already answered, agent mid-reply, fast mode
    // already off) short-circuit ahead of the one gate that is not cheap -- the
    // turn count, which walks the held transcript. Which agents owe the prompt
    // at all is the harness's declaration (the fast_mode_prompt popup on its
    // catalog), not a harness-name check here.
    maybePromptForFastMode(agent, events, agentIsIdle);

    // Memoize the turn-grouping -> rows pipeline. buildSections walks the entire
    // held transcript, so recomputing it on every scroll-driven redraw is the
    // dominant scroll cost on a long conversation. Its output depends only on the
    // held events and the idle flag -- captured by the render version (bumped on
    // any data mutation) plus the idle flag -- so a scroll-only redraw reuses the
    // cached rows. The grouping (steps, decoration, skill expansions, auth-error
    // hiding) is produced by the same functions on the same inputs, so the
    // rendered structure is identical to recomputing.
    const renderKey = `${agentId}|${getRenderVersion(agentId)}|${agentIsIdle ? 1 : 0}`;
    if (renderKey !== rowsCacheKey) {
      // Both structure and decoration come from the transcript walk; there is no
      // side-channel enrichment. The same pipeline feeds the subagent view, so a
      // subagent's "View conversation" renders an identical progress timeline.
      cachedRows = buildConversationRows(agentId, events, agentIsIdle, getFirstOffset(agentId));
      cachedKeyToIndex = new Map(cachedRows.map((row, index) => [row.key, index]));
      measurements.prune(new Set(cachedRows.map((row) => row.key)));
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;

    // Fold every settled row into the conversation's geometry, so the reserved
    // space below reflects real heights. Only settled rows are admitted: a height
    // read before markdown and highlighting land is a placeholder, and persisting
    // it would make the estimate permanently wrong.
    syncGeometryToWidth(agentId, scroll.scrollEl?.clientWidth ?? 0);
    if (recordSettledGeometry(rows)) {
      scheduleGeometrySave(agentId);
    }

    // Reserve space above and below the loaded window for history that exists on
    // the server but isn't loaded yet, so the scrollbar reflects the whole
    // conversation rather than just the loaded window. Both numbers come from
    // measured geometry, falling back to a rate learned from this transcript's own
    // rows for ranges never rendered -- so what a page reserves and what it lands
    // at are the same quantity, and paging one in does not shift the scrollbar.
    const total = getTotalEventCount(agentId);
    const firstOffset = getFirstOffset(agentId);
    const windowEnd = firstOffset + events.length;
    updateReserveRate(rows);
    phantomTopHeight = Math.round(geometry.heightBefore(firstOffset, reserveRate));
    phantomBottomHeight = Math.round(
      Math.max(0, geometry.heightBefore(total, reserveRate) - geometry.heightBefore(windowEnd, reserveRate)),
    );

    // Rows a live selection touches, kept mounted by the virtualizer's range
    // extractor even when the viewport has moved far away -- removing a
    // selection endpoint's node collapses the selection.
    pinnedRowIndices = selectionActive ? resolveSelectionRowIndices(scroll.scrollEl, cachedKeyToIndex) : [];

    captureReadingAnchor();
    virtualizer.sync();
    const items = virtualizer.getVirtualItems();

    return m("div", { class: "message-list-wrapper" }, [
      // The queued-message group renders after the virtualized rows so it sits at
      // the live tail, below the last committed turn. It is a full snapshot from
      // the harness, replaced wholesale on each push.
      m("div", { class: MESSAGE_LIST_CLASS }, [
        ...renderVirtualRows(rows, items, virtualizer.getTrailingSpace()),
        ...renderQueuedMessages(agentId),
        ...renderOutgoingMessages(agentId),
      ]),
    ]);
  }

  /**
   * Pixels per event used to price history this client has never rendered.
   *
   * Deliberately not read off the geometry index, which only admits rows that
   * have *settled* -- half a second after paint, and only folded in on whatever
   * redraw happens next. Pricing the reserve off that meant it sat at the cold
   * default until the first settle landed and then collapsed several-fold in one
   * step, and that step routinely fell on the user's own scroll: the shorter
   * content clamped scrollTop, which re-armed tail-following underneath them.
   *
   * Taking the rate from every row that has been measured at all converges it
   * within a frame of first paint, while the viewport is still pinned to the
   * tail and the change is invisible.
   */
  let reserveRate = DEFAULT_EVENT_HEIGHT_PX;
  // Set once enough rows have been measured to trust the rate; from then on the
  // reserved height only changes when the window itself does.
  let isReserveRateSettled = false;

  /**
   * Re-learn the reserve rate, but only when it has really moved.
   *
   * The relative threshold is what stops the reserve churning: once converged,
   * ordinary measurement noise leaves it alone, so the scroll height stays put
   * and the scrollbar does not crawl. A genuine change -- the cold default
   * meeting reality, or a conversation whose character shifts -- clears it
   * easily.
   */
  function updateReserveRate(rows: RowDescriptor[]): void {
    // Weighted across the WHOLE loaded window, counting a row's estimate when it
    // has not been measured yet. Taking a median over only the measured rows
    // instead made the rate lurch as different kinds of row settled at different
    // times -- short user bubbles first, taller assistant rows after -- which
    // moved the reserve by thousands of pixels seconds after load. Including
    // every row keeps the mix representative from the first frame, so the rate
    // only tightens as estimates are replaced by measurements rather than
    // swinging.
    // A hidden panel has nothing laid out, so it can learn nothing -- and
    // re-pricing the reserve then would move the content behind a tab the reader
    // is not looking at, so it would come back somewhere else than they left it.
    if (!panelVisible || isReserveRateSettled) {
      return;
    }
    let totalHeight = 0;
    let totalEvents = 0;
    let measuredRows = 0;
    for (const row of rows) {
      const events = row.end_offset - row.start_offset;
      if (events <= 0) {
        continue;
      }
      const measured = measurements.heightFor(row.key);
      if (measured !== undefined) {
        measuredRows += 1;
      }
      totalHeight += measured ?? row.estimate;
      totalEvents += events;
    }
    if (totalEvents <= 0 || totalHeight <= 0) {
      return;
    }
    const observed = totalHeight / totalEvents;
    if (Math.abs(observed - reserveRate) / reserveRate > RESERVE_RATE_CHANGE_THRESHOLD) {
      reserveRate = observed;
    }
    if (measuredRows >= RESERVE_RATE_SAMPLE_ROWS) {
      isReserveRateSettled = true;
    }
  }

  /**
   * The geometry stored for this conversation at this width, from whichever tier
   * has it.
   *
   * Two tiers because they answer different questions. IndexedDB is what *this*
   * browser measured and costs no request, so it is asked first. The workspace's
   * copy covers a conversation this browser has never rendered but another
   * window -- or another device at the same width -- already measured, which is
   * the difference between landing on accurate geometry and settling into it.
   */
  async function loadGeometrySnapshot(agentId: string, bucket: number): Promise<GeometrySnapshot | null> {
    const cached = await sharedGeometryCache.load(agentId, bucket);
    if (cached !== null) {
      return cached;
    }
    return loadWorkspaceGeometry(agentId, bucket);
  }

  /**
   * Adopt the persisted geometry for this agent at this viewport width.
   *
   * Heights are a function of width, so a width change is a genuine cache miss:
   * the measurements in hand describe a layout that no longer exists, and are
   * dropped rather than reused. Bucketing means the common small changes (a
   * scrollbar appearing, a few pixels of panel resize) stay warm.
   *
   * Every measured height goes, not just the persisted ones. The virtualizer
   * keeps its own size cache and only asks `estimateSize` for a row it has no
   * size for, so leaving it holding the old width's heights would mean each
   * off-screen row snapping as it came back into view. The learned per-event
   * rate goes with them: it is settled-and-frozen by design, so a stale one
   * would price unloaded history at the old width for the panel's lifetime.
   *
   * The load is async and may land after the user has already scrolled. That is
   * safe: adopting it changes the reserved height, and restoreReadingAnchor puts
   * the reader back on the row they were reading, so the content in front of
   * them does not move.
   */
  function syncGeometryToWidth(agentId: string, width: number): void {
    const bucket = widthBucketFor(width);
    if (bucket === geometryWidthBucket || width <= 0) {
      return;
    }
    geometryWidthBucket = bucket;
    measurements.reset();
    geometry = new RowGeometryIndex();
    virtualizer.reset();
    reserveRate = DEFAULT_EVENT_HEIGHT_PX;
    isReserveRateSettled = false;
    const requestedAgentId = agentId;
    const requestedBucket = bucket;
    loadGeometrySnapshot(agentId, bucket)
      .then((snapshot) => {
        // Discard a load that lost a race with an agent switch or another
        // resize; its numbers describe a layout or conversation we left.
        if (snapshot === null || currentAgentId !== requestedAgentId || geometryWidthBucket !== requestedBucket) {
          return;
        }
        geometry = geometryFromSnapshot(snapshot);
        m.redraw();
      })
      .catch(() => {
        // A cache miss is not a failure worth surfacing: the transcript renders
        // from estimates and re-measures as it goes.
      });
  }

  /** Persist the conversation's geometry to both tiers, coalesced so a burst of
   *  settling rows costs one write rather than one per row. */
  function scheduleGeometrySave(agentId: string): void {
    if (geometrySaveTimer !== null) {
      return;
    }
    geometrySaveTimer = setTimeout(() => {
      geometrySaveTimer = null;
      if (currentAgentId !== agentId || geometry.rowCount === 0) {
        return;
      }
      const snapshot = geometry.toSnapshot();
      void sharedGeometryCache.save(agentId, geometryWidthBucket, snapshot).catch(() => {
        // Both tiers are fire-and-forget for the same reason: geometry is an
        // optimisation, so a write that does not land costs one settling pass on
        // the next visit and nothing a reader can see.
      });
      // The workspace's copy, so the next window to open this conversation --
      // this browser or another -- does not have to measure it again.
      void saveWorkspaceGeometry(agentId, geometryWidthBucket, snapshot);
    }, GEOMETRY_SAVE_DEBOUNCE_MS);
  }

  /** Drop a pending save. The conversation it was armed for is going away, so its
   *  snapshot is about to be replaced by an empty index anyway, and leaving the
   *  timer armed would hold the "already scheduled" slot against the next one. */
  function cancelGeometrySave(): void {
    if (geometrySaveTimer !== null) {
      clearTimeout(geometrySaveTimer);
      geometrySaveTimer = null;
    }
  }

  /**
   * Fold settled row measurements into the conversation's geometry.
   *
   * A row is admitted only once its height has gone quiet, so a placeholder
   * measured before markdown or syntax highlighting lands is never recorded as
   * the real height. Rows whose range is degenerate are skipped: they carry no
   * events of their own and would corrupt the prefix sums.
   */
  function recordSettledGeometry(rows: RowDescriptor[]): boolean {
    // A hidden panel has nothing laid out, so nothing it could learn is worth
    // learning -- and refining the geometry then would move the reserved space
    // under a reader who is not there to see it, so the tab would come back in a
    // different place than they left it.
    if (!panelVisible) {
      return false;
    }
    const now = Date.now();
    let changed = false;
    for (const row of rows) {
      if (row.end_offset <= row.start_offset) {
        continue;
      }
      const height = measurements.heightFor(row.key);
      if (height === undefined || !measurements.isSettled(row.key, now)) {
        continue;
      }
      if (
        geometry.recordRow({
          row_key: row.key,
          start_offset: row.start_offset,
          end_offset: row.end_offset,
          height,
        })
      ) {
        changed = true;
      }
    }
    return changed;
  }

  const handleAgentsUpdated = (): void => retryAfterAgentResolved();

  return {
    oninit() {
      addAgentsUpdatedListener(handleAgentsUpdated);
    },

    onremove() {
      removeAgentsUpdatedListener(handleAgentsUpdated);
      disconnectLogWs();
      scroll.detach();
      virtualizer.unmount();
      cancelGeometrySave();
      if (currentAgentId !== null) {
        disconnectFromStream(currentAgentId);
      }
    },

    view(vnode) {
      const agentId = vnode.attrs.agentId;
      // dockview's live visibility for this panel, fed in by the renderer. Read
      // it before building content / running lifecycle hooks so the scroll hooks
      // (which read this closure variable) see the current value. Undefined for a
      // mount without a panel api -- treat that as visible.
      panelVisible = vnode.attrs.isVisible ?? true;

      // renderMessages sets the reserved heights, so build the content first, then
      // decide whether the viewport currently sits over a reserved region (above
      // all loaded rows, or below them) and so should show a loading overlay
      // instead of a blank spacer while the fetch for that region lands.
      const content = isSlotClaimed("conversation-content") ? null : renderMessages(agentId);
      const scrollEl = scroll.scrollEl;
      const currentScrollTop = scroll.scrollTop;
      const viewportPx = scroll.viewportHeight > 0 ? scroll.viewportHeight : (scrollEl?.clientHeight ?? 0);
      const loadedTop = phantomTopHeight;
      const loadedBottom = scrollEl !== null ? scrollEl.scrollHeight - phantomBottomHeight : Number.MAX_SAFE_INTEGER;
      const inReservedRegion =
        (phantomTopHeight > 0 && currentScrollTop < loadedTop) ||
        (phantomBottomHeight > 0 && currentScrollTop + viewportPx > loadedBottom);

      const acceptsFileDrops = !isProtoAgent(agentId) && !isConversationNotFound(agentId);

      return m(
        "div",
        {
          class: "chat-panel flex flex-col h-full relative",
          ondragenter: acceptsFileDrops ? handleDragEnter : undefined,
          ondragover: acceptsFileDrops ? handleDragOver : undefined,
          ondragleave: acceptsFileDrops ? handleDragLeave : undefined,
          ondrop: acceptsFileDrops ? (event: DragEvent) => handleDrop(event, agentId) : undefined,
        },
        [
          isFileDragActive && acceptsFileDrops
            ? m(
                "div",
                { class: "chat-drop-overlay absolute inset-0 flex items-center justify-center pointer-events-none" },
                m("div", { class: "chat-drop-overlay-label" }, "Drop files to attach"),
              )
            : null,
          m(
            "main",
            {
              class: "app-content flex-1 overflow-y-auto px-8 py-6",
              onscroll: (event: Event) => scroll.onScroll(event),
              // Mark the start of a drag (likely a selection) so the tail-follow pin
              // defers while the button is held (see the controller's applyTailFollow).
              onpointerdown: () => scroll.onPointerDown(),
              oncreate: (mainVnode: m.VnodeDOM) => {
                const element = mainVnode.dom as HTMLElement;
                scroll.attach(element);
                virtualizer.mount();
                restoreReadingAnchor(element);
                applyScrollPosition(element);
                measureScheduler.schedule();
                if (currentAgentId !== null) {
                  maybePage(currentAgentId, element);
                }
              },
              onupdate: (mainVnode: m.VnodeDOM) => {
                const element = mainVnode.dom as HTMLElement;
                scroll.attach(element);
                virtualizer.mount();
                restoreReadingAnchor(element);
                applyScrollPosition(element);
                measureScheduler.schedule();
                // Drive paging from the render loop, not only from scroll events, so
                // the viewport sitting over a reserved region always triggers (or
                // already has in flight) the fetch to cover it. Without this a drag
                // that ends in a reserved region -- with the triggering scroll event
                // suppressed by an in-flight fetch -- could strand the loading overlay
                // with nothing actually loading.
                if (currentAgentId !== null) {
                  maybePage(currentAgentId, element);
                }
              },
            },
            content,
          ),
          // While the viewport is over reserved space for not-yet-loaded history
          // (e.g. the scrollbar was dragged into a region the loaded window doesn't
          // cover yet), overlay a loading indicator centered in the viewport so the
          // user never sees a blank area. pointer-events:none so it never blocks scroll.
          inReservedRegion
            ? m(
                "div",
                {
                  class:
                    "message-list-window-loading absolute inset-0 flex items-center justify-center p-6 pointer-events-none",
                },
                m("p", { class: "text-text-secondary" }, "Loading messages..."),
              )
            : null,
          // Only show message input when not in proto-agent mode
          isProtoAgent(agentId)
            ? null
            : m("footer", { class: "app-footer" }, [
                m(EmptySlot, { name: "conversation-before-input" }),
                isConversationNotFound(agentId)
                  ? null
                  : m(ActivityIndicator, {
                      agentId,
                      events: getEventsForAgent(agentId),
                    }),
                m(MessageInput, { agentId }),
                // Below the chat input: the original flex row -- model bar on the left, the
                // agent-terminal + harness-auth actions right-aligned. The "Powered by" credit is
                // rendered last as a centered overlay (absolute, pointer-events:none) so it sits
                // in the middle without reshaping the row. Shared font, no background of its own.
                m("div", { class: "composer-under-bar" }, [
                  m(ModelBar, { agentId }),
                  m("div", { class: "composer-under-bar-actions" }, [
                    m(
                      "button",
                      {
                        type: "button",
                        class: "composer-under-bar-action",
                        onclick: () => openAgentTerminalTab(agentId),
                      },
                      "Open agent terminal",
                    ),
                    // Persistent entry to the sign-in modal so the user can switch
                    // auth modes without waiting for an auth error.
                    m(
                      "button",
                      { type: "button", class: "composer-under-bar-action", onclick: () => openAgentAuth(agentId) },
                      "Agent auth",
                    ),
                  ]),
                  // The centered harness credit (may render nothing), overlaid on the bar.
                  m(PoweredByCredit, { agentId }),
                ]),
              ]),
        ],
      );
    },
  };
}
