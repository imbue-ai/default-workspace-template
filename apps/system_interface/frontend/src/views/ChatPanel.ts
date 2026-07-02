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
import { computeVisibleWindow } from "../models/virtualWindow";
import { nextUserScrolledUp, isSelectionActiveWithin } from "../models/scrollFollow";
import { createRowMeasurer, OVERSCAN_PX } from "./row-measurement";
import {
  captureTopAnchor,
  contentTopOfRow,
  resolveSelectionRowRange,
  selectionStateWithin,
  SELECTION_PIN_MAX_GAP_ROWS,
  type ScrollAnchor,
} from "./scroll-selection";
import { connectToStream, disconnectFromStream, loadSnapshotWithStream } from "../models/StreamingMessage";
import { getAgentById, getProtoAgents } from "../models/AgentManager";
import { openLoginModal } from "../models/ClaudeAuth";
import { apiUrl } from "../base-path";
import { EmptySlot } from "./EmptySlot";
import { MessageInput } from "./MessageInput";
import { buildAgentTerminalUrl, getTerminalUrl, openIframeTabForAgent } from "./DockviewWorkspace";
import { buildConversationRows, type RowDescriptor } from "./conversation-rows";
import { ActivityIndicator } from "./ActivityIndicator";
import { renderPendingMessages } from "./PendingMessageView";

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

const SCROLL_BOTTOM_THRESHOLD_PX = 40;

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
// Stable per-event height used to size the reserved (phantom) regions for history
// that exists on the server but isn't loaded yet. It is deliberately a constant
// rather than the measured average of the loaded window: the loaded window is a
// tiny fraction of a long transcript (e.g. 50 of 5000+ events), so its measured
// average -- which shifts every frame as rows measure -- would be amplified by the
// large unloaded count into wild scrollbar jumps. A constant keeps the total
// scroll height (~ total * this) stable, so the scrollbar thumb doesn't churn and
// an offset jump lands at a fixed position. Its exact value isn't UX-critical:
// the drag fraction -> event index mapping and the post-jump thumb position both
// scale with it and so are independent of it; only the loaded window's small
// residual (measured height vs count * this) is affected.
const ESTIMATED_EVENT_HEIGHT_PX = 160;

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < SCROLL_BOTTOM_THRESHOLD_PX;
}

function scrollToBottom(element: HTMLElement): void {
  element.scrollTop = element.scrollHeight;
}

function isProtoAgent(agentId: string): boolean {
  return getProtoAgents().some((p) => p.agent_id === agentId);
}

export function ChatPanel(): m.Component<{ agentId: string; isVisible?: boolean }> {
  let loading = false;
  let loadingError: string | null = null;
  let currentAgentId: string | null = null;
  let userScrolledUp = false;

  // Virtualization state.
  let scrollEl: HTMLElement | null = null;
  let viewportHeight = 0;
  let scrollTop = 0;
  // Previous observed scroll position, for detecting scroll direction. Updated in
  // lockstep with scrollTop at every programmatic scroll site (see handleScrollEvent).
  let previousScrollTop = 0;
  const rowMeasurer = createRowMeasurer();
  let viewportResizeObserver: ResizeObserver | null = null;
  // Whether this panel is the visible (selected) tab in its dockview group.
  // dockview keeps an inactive tab mounted (defaultRenderer: "always") and
  // mithril redraws globally, so the component keeps running while hidden
  // against an element collapsed to zero size; running scroll work then would
  // corrupt the retained scroll position. The renderer feeds dockview's
  // authoritative visibility in via the ``isVisible`` attr (see
  // createMithrilRenderer); the scroll hooks below skip while it is false.
  // Defaults to true so the panel works before the first render sets it.
  let panelVisible = true;
  // Memoized turn-grouping output. buildSections walks the whole held
  // transcript, so it is recomputed only when the data actually changes (keyed
  // on the render version + idle flag), not on every scroll-driven redraw.
  let rowsCacheKey: string | null = null;
  let cachedRows: RowDescriptor[] = [];
  // Row key -> index in cachedRows, memoized alongside it. Used to resolve a live
  // selection's DOM rows to virtualization indices so they can be pinned into the
  // window (see renderMessages).
  let cachedKeyToIndex = new Map<string, number>();
  // Heights reserved above/below the loaded window for history that exists on the
  // server but isn't loaded yet (see renderMessages). Shared so the scroll handler
  // can tell when the viewport is over a reserved region and page/jump/overlay
  // accordingly.
  let phantomTopHeight = 0;
  let phantomBottomHeight = 0;
  // Paging (scroll-driven fetch) in-flight guard. Covers older/newer pages and
  // offset jumps -- only one is outstanding at a time.
  let backfillInFlight = false;
  // After an offset jump replaces the window, pin the viewport once to the top of
  // the freshly loaded rows (just below the top reserved spacer) so the user lands
  // on the jumped-to content rather than in the reserved region above it. With the
  // reserved heights now sized by a stable constant, the top of the loaded window
  // doesn't drift as rows measure, so a single pin suffices -- no timed settle.
  let pendingPinToWindowTop = false;
  // Row-key scroll anchor for the scrolled-up case: keep the row at the top of
  // the viewport visually fixed as content above it changes height (a backfill
  // prepend landing, or an off-screen row measuring to its real height). This
  // replaces the old "capture scrollHeight before the fetch, diff it after"
  // compensation, which mis-attributed unrelated height changes (streaming tail
  // appends, measure-pass corrections) to the prepend and could park the viewport
  // exactly in the next backfill trigger band -- a self-sustaining up/down yank.
  let scrollAnchor: ScrollAnchor | null = null;
  // Last observed scrollHeight, to distinguish a browser shrink-clamp (content got
  // shorter, so scrollTop was pushed up to the new max) from a genuine user
  // scroll-up when updating the follow state.
  let lastScrollHeight = 0;
  // A pointer button is held down over the transcript (the user is mid-drag,
  // likely selecting). The tail-follow pin is deferred while this holds so
  // streaming output doesn't scroll content out from under the drag; it resumes
  // the instant the button is released. Unlike a scroll-up, this never disengages
  // follow.
  let isPointerDown = false;
  // Window-level listener that clears isPointerDown on release (the pointer may be
  // released outside the panel). Registered on mount, removed on unmount.
  let pointerReleaseListener: (() => void) | null = null;

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
          openLoginModal();
        }
        return;
      }
    }
  }

  // Screen capture state (shown when agent has no conversation)
  let screenContent: string | null = null;
  let screenError: string | null = null;
  let screenLoading = false;
  let screenAgentId: string | null = null;

  // Proto-agent log state
  let logWs: WebSocket | null = null;
  let logLines: string[] = [];
  let logDone = false;
  let logSuccess = false;
  let logError: string | null = null;
  let logAgentId: string | null = null;

  async function fetchScreenCapture(agentId: string): Promise<void> {
    if (screenAgentId === agentId && (screenContent !== null || screenLoading)) {
      return;
    }
    screenAgentId = agentId;
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
        { line: string } | { done: true; success: boolean; error: string | null };

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
        loadingError = (error as Error).message ?? String(error);
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
    scrollTop = 0;
    previousScrollTop = 0;
    userScrolledUp = false;
    backfillInFlight = false;
    scrollAnchor = null;
    lastScrollHeight = 0;
    rowMeasurer.reset();
    loadAgent(agentId);
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
    // jump; over the loaded rows the edge-paging branches below handle it. The old
    // global-fraction mapping assumed scrollHeight ~= total * ESTIMATED_EVENT_HEIGHT_PX,
    // so measured-height divergence in the loaded window could push the estimate
    // across the jump threshold and fire a spurious window reset (which unmounts
    // every row -- the most violent scroll jolt, and a guaranteed selection kill).
    const loadedBottom = element.scrollHeight - phantomBottomHeight;
    let targetIndex: number | null = null;
    if (phantomTopHeight > 0 && element.scrollTop < phantomTopHeight) {
      targetIndex = Math.round(element.scrollTop / ESTIMATED_EVENT_HEIGHT_PX);
    } else if (phantomBottomHeight > 0 && element.scrollTop + element.clientHeight > loadedBottom) {
      const intoBottomRegion = element.scrollTop + element.clientHeight - loadedBottom;
      targetIndex = windowEnd + Math.round(intoBottomRegion / ESTIMATED_EVENT_HEIGHT_PX);
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
        userScrolledUp = true;
        pendingPinToWindowTop = true;
        m.redraw();
      });
      return;
    }

    // Near the top of the loaded rows -> page older. The scroll anchor keeps the
    // viewport fixed when the older page lands above (see applyScrollAnchor).
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

  function applyScrollPosition(element: HTMLElement): void {
    // While the panel is hidden (an inactive dockview tab) the element is
    // zero-sized. Re-pinning or scrolling-to-bottom now would set scrollTop from
    // a 0 scrollHeight and clobber the retained scrollTop/previousScrollTop to 0,
    // losing the user's place. The browser preserves the native scrollTop across
    // a hide/show, so skipping here keeps the position intact; dockview fires a
    // visibility change on show that forces a redraw (see createMithrilRenderer),
    // so this runs again then and restores the tail / scrolled-up position.
    if (!panelVisible) {
      return;
    }
    // After an offset jump, pin the viewport once to the top of the freshly loaded
    // rows (just below the top reserved spacer) so the user lands on the jumped-to
    // content rather than in the reserved (blank) region above it. The reserved
    // top height is a stable constant * offset, so it doesn't drift as the loaded
    // rows measure -- a single pin lands correctly without a timed settle.
    if (pendingPinToWindowTop) {
      pendingPinToWindowTop = false;
      element.scrollTop = phantomTopHeight;
      scrollTop = element.scrollTop;
      previousScrollTop = element.scrollTop;
      lastScrollHeight = element.scrollHeight;
      scrollAnchor = null;
      return;
    }

    // The app is the single owner of scrollTop (native scroll anchoring is turned
    // off via `overflow-anchor: none` in the stylesheet). While scrolled up, hold
    // the anchored row fixed against height changes above it; while following, pin
    // to the tail. The two are mutually exclusive.
    if (userScrolledUp) {
      applyScrollAnchor(element);
    } else {
      applyTailFollow(element);
    }
    lastScrollHeight = element.scrollHeight;
  }

  // Keep the anchored row visually fixed by shifting scrollTop by exactly the
  // amount the row moved in scroll-content space since the anchor was captured.
  // Relative (not absolute) so any user scroll since the last frame is preserved:
  // an absolute re-set per redraw would erase in-flight wheel movement and bring
  // back the fighting-the-scrollbar feel.
  function applyScrollAnchor(element: HTMLElement): void {
    if (scrollAnchor === null) {
      return;
    }
    const currentTop = contentTopOfRow(element, scrollAnchor.key);
    if (currentTop === null) {
      // The anchor row is gone (re-keyed, evicted, or scrolled out of the window).
      // Drop it; the next scroll event re-captures. Never treat "not found" as 0.
      scrollAnchor = null;
      return;
    }
    const delta = currentTop - scrollAnchor.contentTop;
    if (delta !== 0) {
      element.scrollTop = element.scrollTop + delta;
    }
    // Content-space top is invariant under the scrollTop write above, so it is the
    // new baseline.
    scrollAnchor = { key: scrollAnchor.key, contentTop: currentTop };
    scrollTop = element.scrollTop;
    previousScrollTop = element.scrollTop;
  }

  function applyTailFollow(element: HTMLElement): void {
    // Mid-drag: hold position so a selection drag isn't chasing auto-scroll. Does
    // not disengage follow -- it resumes on the next redraw once the button is up.
    if (isPointerDown) {
      return;
    }
    // Honor an unprocessed user wheel-up whose scroll event hasn't fired yet: if
    // the live scrollTop sits above where we last pinned, the user is scrolling up,
    // so stop pinning (otherwise a streaming redraw yanks it back before the scroll
    // event registers -- input swallowed, "fighting the scrollbar"). The
    // `min(scrollTop, maxScroll)` guard distinguishes this from the browser
    // clamping scrollTop after the content shrank (eviction, a turn collapsing),
    // which is still-at-bottom and must keep following.
    const maxScroll = element.scrollHeight - element.clientHeight;
    if (element.scrollTop < Math.min(scrollTop, maxScroll) - 1) {
      userScrolledUp = true;
      scrollTop = element.scrollTop;
      previousScrollTop = element.scrollTop;
      scrollAnchor = null;
      return;
    }
    scrollToBottom(element);
    scrollTop = element.scrollTop;
    previousScrollTop = element.scrollTop;
  }

  function handleScrollEvent(event: Event): void {
    const element = event.target as HTMLElement;
    // applyScrollPosition keeps previousScrollTop in lockstep with its own
    // programmatic re-pins, so only a genuine user scroll registers as movement.
    const didScrollUp = element.scrollTop < previousScrollTop;
    const atBottom = isNearBottom(element);
    // A shrink-clamp: the content got shorter and the browser pushed scrollTop up
    // to the new max. It looks like a scroll-up but carries no user intent, so the
    // follow state must be preserved rather than re-derived (see scrollFollow).
    const isClamp = didScrollUp && element.scrollHeight < lastScrollHeight && atBottom;
    previousScrollTop = element.scrollTop;
    scrollTop = element.scrollTop;

    userScrolledUp = nextUserScrolledUp({
      didScrollUp,
      isNearBottom: atBottom,
      hasMoreAfter: hasMoreAfter(currentAgentId ?? ""),
      isClamp,
      wasUserScrolledUp: userScrolledUp,
    });

    // Re-anchor to the row now at the top of the viewport so later height changes
    // above it keep it fixed; clear the anchor when following the tail.
    scrollAnchor = userScrolledUp ? captureTopAnchor(element) : null;
    lastScrollHeight = element.scrollHeight;

    if (currentAgentId !== null) {
      maybePage(currentAgentId, element);
    }
  }

  // Refresh the cached viewport height and schedule a measure pass. Kept local
  // so the viewport height (used by the window math below) stays current; the
  // measure/cache mechanics themselves live in the shared row measurer.
  function scheduleMeasure(): void {
    // While the panel is hidden the element measures 0; don't overwrite the
    // retained viewportHeight with 0 (the windowing math would then fall back to
    // a wrong height). The row measurer already ignores zero-height rows, so it
    // is safe to schedule regardless.
    if (scrollEl !== null && panelVisible) {
      viewportHeight = scrollEl.clientHeight;
    }
    rowMeasurer.scheduleMeasure(() => scrollEl);
  }

  // Union the scroll-anchor row (while scrolled up) into a pinned range so it stays
  // mounted for applyScrollAnchor to measure. The anchor is the top visible row, so
  // it is never subject to the selection gap cap.
  function withAnchorPinned(range: { start: number; end: number } | null): { start: number; end: number } | null {
    if (!userScrolledUp || scrollAnchor === null) {
      return range;
    }
    const anchorIndex = cachedKeyToIndex.get(scrollAnchor.key);
    if (anchorIndex === undefined) {
      return range;
    }
    if (range === null) {
      return { start: anchorIndex, end: anchorIndex };
    }
    return { start: Math.min(range.start, anchorIndex), end: Math.max(range.end, anchorIndex) };
  }

  function renderMessages(agentId: string): m.Vnode {
    // Reset here so the loading overlay (keyed on a positive value) stays hidden
    // for every path that doesn't render the windowed list; the windowed path
    // below sets the real reserved heights.
    phantomTopHeight = 0;
    phantomBottomHeight = 0;

    // If this agent is still being created, show the build log
    if (isProtoAgent(agentId)) {
      return renderBuildLog(agentId);
    }

    // Creation completed but failed -- keep the build log visible so the
    // user can read the error and the last few log lines. Without this the
    // build-log view transitions to the empty-chat / "no conversation data"
    // screen the instant proto_agent_completed arrives and the error flashes
    // by unreadably. The agent will never be added to getAgents() on
    // failure, so nothing else in the UI would surface the error either.
    if (logAgentId === agentId && logDone && !logSuccess) {
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
    const selectionActive = isSelectionActiveWithin(selectionStateWithin(scrollEl));

    // Bound client memory while following the live tail: trim the oldest held
    // events once well over the cap. Only when at the bottom, so a scrolled-up
    // reader's rendered history is never yanked out from under them; the dropped
    // history is re-fetched via backfill on scroll-up (evictOldEvents advances the
    // window start so it reads as older history above). Re-pinned to the bottom by
    // applyScrollPosition afterwards. Also skipped while a selection is active:
    // eviction deletes the underlying events, which no amount of DOM pinning can
    // survive. This temporarily lifts the MAX_HELD_EVENTS bound while a selection
    // is held; it is restored on the first redraw after the selection is dropped.
    if (!userScrolledUp && !selectionActive && getEventCount(agentId) > MAX_HELD_EVENTS) {
      evictOldEvents(agentId);
    }

    const events = getEventsForAgent(agentId);

    if (events.length === 0) {
      // No transcript yet -- but the user may have just sent their first
      // message, which should still show immediately as an optimistic bubble
      // rather than be hidden behind the empty-state placeholder.
      const pendingNodes = renderPendingMessages(agentId);
      if (pendingNodes.length === 0) {
        return m(
          "div",
          { class: "message-list-empty flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "No events yet for this agent."),
        );
      }
      return m("div", { class: "message-list-wrapper" }, [m("div", { class: MESSAGE_LIST_CLASS }, pendingNodes)]);
    }

    const agent = getAgentById(agentId);
    const agentIsIdle = agent?.activity_state === "IDLE";

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
      cachedRows = buildConversationRows(agentId, events, agentIsIdle);
      cachedKeyToIndex = new Map(cachedRows.map((row, index) => [row.key, index]));
      rowMeasurer.prune(new Set(cachedRows.map((row) => row.key)));
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;

    const getHeight = (index: number): number => rowMeasurer.getHeight(rows[index].key) ?? rows[index].estimate;

    // Reserve space above and below the loaded window for history that exists on
    // the server but isn't loaded yet, so the scrollbar reflects the whole
    // conversation rather than just the loaded window -- and so paging more in
    // doesn't make it jump. Each reserve is the count of not-yet-loaded events on
    // that side times a stable per-event constant (see ESTIMATED_EVENT_HEIGHT_PX).
    // Using a constant (not the loaded window's measured average) is what keeps the
    // total scroll height stable: deriving it from the small loaded window would
    // make every row measurement, amplified by the large unloaded count, jolt the
    // scrollbar. As events page in, the reserve shrinks by ~the height they add, so
    // existing content stays put.
    const total = getTotalEventCount(agentId);
    const firstOffset = getFirstOffset(agentId);
    const olderUnloaded = Math.max(0, firstOffset);
    const newerUnloaded = Math.max(0, total - (firstOffset + events.length));
    phantomTopHeight = Math.round(olderUnloaded * ESTIMATED_EVENT_HEIGHT_PX);
    phantomBottomHeight = Math.round(newerUnloaded * ESTIMATED_EVENT_HEIGHT_PX);

    // The loaded rows start below the top phantom spacer, so shift the scroll
    // position into the loaded rows' own coordinate space for the window math.
    const adjustedScrollTop = Math.max(0, scrollTop - phantomTopHeight);
    // Before the first measure viewportHeight is 0; fall back to the live
    // clientHeight (or a large value) so the initial render is not a 1-row sliver
    // that the post-mount measure then has to expand.
    const effectiveViewportHeight = viewportHeight > 0 ? viewportHeight : (scrollEl?.clientHeight ?? 2000);
    const baseWindow = computeVisibleWindow({
      count: rows.length,
      getHeight,
      scrollTop: adjustedScrollTop,
      viewportHeight: effectiveViewportHeight,
      overscanPx: OVERSCAN_PX,
    });
    // Pin the rows holding a live selection into the window so scrolling or
    // streaming past them doesn't unmount their DOM and collapse the selection.
    // Abandon the pin once the selection is more than SELECTION_PIN_MAX_GAP_ROWS
    // from the viewport, so a selection held through a long stream can't keep an
    // unbounded span mounted (the selection then collapses -- a deliberate bound).
    let pinnedRange = selectionActive ? resolveSelectionRowRange(scrollEl, cachedKeyToIndex) : null;
    if (pinnedRange !== null) {
      const gapAbove = baseWindow.startIndex - pinnedRange.end;
      const gapBelow = pinnedRange.start - baseWindow.endIndex;
      if (gapAbove > SELECTION_PIN_MAX_GAP_ROWS || gapBelow > SELECTION_PIN_MAX_GAP_ROWS) {
        pinnedRange = null;
      }
    }
    // Also keep the scroll-anchor row mounted while scrolled up, so applyScrollAnchor
    // can always measure it and compensate for a prepend/measurement in the same
    // frame -- otherwise a page that shrinks the top phantom more than its rows add
    // could slide the window off the anchor for one frame and let the viewport jump.
    pinnedRange = withAnchorPinned(pinnedRange);
    const windowResult =
      pinnedRange === null
        ? baseWindow
        : computeVisibleWindow({
            count: rows.length,
            getHeight,
            scrollTop: adjustedScrollTop,
            viewportHeight: effectiveViewportHeight,
            overscanPx: OVERSCAN_PX,
            pinnedRange,
          });

    const visibleRows: m.Children[] = [];
    visibleRows.push(m("div", { key: "__spacer_top", style: `height: ${phantomTopHeight + windowResult.topPad}px` }));
    for (let i = windowResult.startIndex; i < windowResult.endIndex; i++) {
      visibleRows.push(rows[i].render());
    }
    visibleRows.push(
      m("div", { key: "__spacer_bottom", style: `height: ${windowResult.bottomPad + phantomBottomHeight}px` }),
    );

    return m("div", { class: "message-list-wrapper" }, [
      // Pending (optimistic) messages render after the virtualized rows so a
      // just-sent bubble shows at the live tail until its real event lands.
      m("div", { class: MESSAGE_LIST_CLASS }, [...visibleRows, ...renderPendingMessages(agentId)]),
    ]);
  }

  return {
    onremove() {
      disconnectLogWs();
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
      const viewportPx = viewportHeight > 0 ? viewportHeight : (scrollEl?.clientHeight ?? 0);
      const loadedTop = phantomTopHeight;
      const loadedBottom = scrollEl !== null ? scrollEl.scrollHeight - phantomBottomHeight : Number.MAX_SAFE_INTEGER;
      const inReservedRegion =
        (phantomTopHeight > 0 && scrollTop < loadedTop) ||
        (phantomBottomHeight > 0 && scrollTop + viewportPx > loadedBottom);

      return m("div", { class: "chat-panel flex flex-col h-full relative" }, [
        m(
          "main",
          {
            class: "app-content flex-1 overflow-y-auto px-8 py-6",
            onscroll: handleScrollEvent,
            // Mark the start of a drag (likely a selection) so the tail-follow pin
            // defers while the button is held (see applyTailFollow).
            onpointerdown: () => {
              isPointerDown = true;
            },
            oncreate: (mainVnode: m.VnodeDOM) => {
              scrollEl = mainVnode.dom as HTMLElement;
              viewportHeight = scrollEl.clientHeight;
              // Clear the mid-drag flag on release. Listen on window, not the panel,
              // because the pointer is often released outside the transcript; redraw
              // so the deferred tail pin re-applies immediately.
              pointerReleaseListener = () => {
                if (isPointerDown) {
                  isPointerDown = false;
                  m.redraw();
                }
              };
              window.addEventListener("pointerup", pointerReleaseListener);
              window.addEventListener("pointercancel", pointerReleaseListener);
              // Recompute the window when the panel itself resizes (dockview
              // splits, window resize) since that changes the visible range. Skip
              // while hidden: dockview collapses an inactive tab's element to 0,
              // and updating viewportHeight from that 0 would break the windowing
              // math. Restoring the position on show is driven by the visibility
              // change forcing a redraw (see createMithrilRenderer), not here.
              viewportResizeObserver = new ResizeObserver(() => {
                if (scrollEl === null || !panelVisible) {
                  return;
                }
                if (scrollEl.clientHeight !== viewportHeight) {
                  viewportHeight = scrollEl.clientHeight;
                  m.redraw();
                }
              });
              viewportResizeObserver.observe(scrollEl);
              applyScrollPosition(scrollEl);
              scheduleMeasure();
              if (currentAgentId !== null) {
                maybePage(currentAgentId, scrollEl);
              }
            },
            onupdate: (mainVnode: m.VnodeDOM) => {
              scrollEl = mainVnode.dom as HTMLElement;
              applyScrollPosition(scrollEl);
              scheduleMeasure();
              // Drive paging from the render loop, not only from scroll events, so
              // the viewport sitting over a reserved region always triggers (or
              // already has in flight) the fetch to cover it. Without this a drag
              // that ends in a reserved region -- with the triggering scroll event
              // suppressed by an in-flight fetch -- could strand the loading overlay
              // with nothing actually loading.
              if (currentAgentId !== null) {
                maybePage(currentAgentId, scrollEl);
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
                : m(ActivityIndicator, { agentId, events: getEventsForAgent(agentId) }),
              m(MessageInput, { agentId }),
              m("div", { class: "chat-agent-terminal-link" }, [
                m(
                  "button",
                  {
                    type: "button",
                    onclick: () => openAgentTerminalTab(agentId),
                  },
                  "Open agent terminal",
                ),
              ]),
            ]),
      ]);
    },
  };
}
