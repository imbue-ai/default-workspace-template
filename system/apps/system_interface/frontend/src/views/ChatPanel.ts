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
  addMessageSentListener,
  evictEvents,
  fetchBackfillEvents,
  fetchEvents,
  fetchForwardEvents,
  fetchWindowAtOffset,
  getConversationLoadState,
  getEventsForAgent,
  getEventCount,
  getFirstOffset,
  getRenderVersion,
  getTotalEventCount,
  isConversationNotFound,
  removeMessageSentListener,
} from "../models/Response";
import type { FillAction } from "../models/transcriptScroll/fillPlanner";
import { createTranscriptScrollEngine } from "./transcript-scroll-engine";
import { TranscriptScrollbar } from "./TranscriptScrollbar";
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
import {
  buildConversationRows,
  MESSAGE_LIST_CLASS,
  renderTranscriptSegments,
  type RowDescriptor,
} from "./conversation-rows";
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

function isProtoAgent(agentId: string): boolean {
  return getProtoAgents().some((p) => p.agent_id === agentId);
}

export function ChatPanel(): m.Component<{ agentId: string; isVisible?: boolean }> {
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
  // Memoized turn-grouping output. buildSections walks the whole held
  // transcript, so it is recomputed only when the data actually changes (keyed
  // on the render version + idle flag), not on every scroll-driven redraw.
  let rowsCacheKey: string | null = null;
  let cachedRows: RowDescriptor[] = [];

  // The scroll engine owns everything about scrolling: the FOLLOW /
  // USER_CONTROLLED state machine, anchor positioning, spacer sizing, the
  // custom scrollbar mapping, progressive fill (paging, jumps, eviction),
  // persistence, and the ?debug=scroll trace. This panel only feeds it data
  // (via the data source below) and renders the rows/spacers it asks for.
  const engine = createTranscriptScrollEngine({
    isVisible: () => panelVisible,
    isStreaming: () => {
      const agent = currentAgentId !== null ? getAgentById(currentAgentId) : undefined;
      return agent !== undefined && agent.activity_state !== "IDLE";
    },
    dataSource: {
      getRows: () => cachedRows,
      getWindowEventIds: () => getEventsForAgent(currentAgentId ?? "").map((event) => event.event_id),
      getFirstOffset: () => getFirstOffset(currentAgentId ?? ""),
      // Null until the first window has been placed (renderVersion bumps on
      // placement, including for an empty transcript), so the engine's fill
      // planner never races the initial snapshot+stream load.
      getTotalEvents: () => {
        const agentId = currentAgentId ?? "";
        return getRenderVersion(agentId) > 0 ? getTotalEventCount(agentId) : null;
      },
      getRenderVersion: () => getRenderVersion(currentAgentId ?? ""),
      executeFill: (action: FillAction): Promise<void> => {
        const agentId = currentAgentId;
        if (agentId === null) {
          return Promise.resolve();
        }
        switch (action.kind) {
          case "fetch-tail":
            return fetchEvents(agentId).then(() => {});
          case "fetch-before":
            return fetchBackfillEvents(agentId, action.limit);
          case "fetch-after":
            return fetchForwardEvents(agentId, action.limit);
          case "fetch-at-offset":
            return fetchWindowAtOffset(agentId, action.offset, action.limit);
          case "evict":
            evictEvents(agentId, action.side, action.count);
            return Promise.resolve();
          case "idle":
            return Promise.resolve();
          default:
            return action satisfies never;
        }
      },
    },
  });

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
    try {
      // Buffer SSE deltas arriving during the snapshot fetch so the wholesale
      // snapshot replace in fetchEvents cannot drop a live event on first load.
      await loadSnapshotWithStream(agentId);
      if (agentId === currentAgentId) {
        checkLatestAssistantForAuthError(agentId);
      }
    } catch (error) {
      // Where the load got to is recorded against the agent by `fetchEvents` and
      // read back in the view, so that a later attempt -- from any caller,
      // including the stream's own reconnect -- supersedes it. Nothing to hold
      // here, and nothing to guard on the agent having been switched away from:
      // the record is per-agent, so a stale load cannot speak for the new one.
      // Still logged, as the paging and reconnect paths do -- an attempt that a
      // newer one has superseded is recorded nowhere at all, so the log is the
      // only trace of one that keeps losing the race.
      console.warn(`Failed to load the transcript for agent ${agentId}`, error);
    }
  }

  // A user-initiated reload is outstanding; guards against stacking them.
  let reloadInFlight = false;

  /**
   * Re-run the load that the panel is currently reporting a failure for.
   *
   * Identical to what the tab menu's Refresh does, offered where the user is
   * already looking: an error screen whose only remedy lives behind a menu they
   * have no particular reason to open reads as a dead end. Redraws on settle
   * because a *failed* reload writes only the load state, which no redraw
   * follows on its own (a successful one repaints when it places the window).
   */
  function reloadAfterFailure(agentId: string): void {
    if (reloadInFlight) {
      return;
    }
    reloadInFlight = true;
    loadAgent(agentId).finally(() => {
      reloadInFlight = false;
      m.redraw();
    });
  }

  const RELOAD_BUTTON_CLASS =
    "message-list-reload cursor-pointer rounded-md border border-border px-3 py-1 text-sm " +
    "text-text-primary hover:bg-bg-hover";

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
    // Resets all scroll state and loads this agent's persisted position (which
    // then steers the engine's fill toward it once the snapshot lands).
    engine.setAgent(agentId);
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

  function renderMessages(agentId: string): m.Vnode {
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

    // A message the user just sent counts as content even before any event
    // exists for it: it may be queued (the harness parked it) or still in flight
    // (an optimistic "Sending…" bubble). All three whole-panel states below are
    // about having nothing to show, so they share one answer -- otherwise a
    // reload firing under a fresh chat replaces that bubble with a spinner or an
    // error screen.
    const tailNodes =
      getEventCount(agentId) === 0 ? [...renderQueuedMessages(agentId), ...renderOutgoingMessages(agentId)] : [];
    const hasNothingToShow = getEventCount(agentId) === 0 && tailNodes.length === 0;

    // Read per-render rather than latched at load time, so the panel leaves the
    // error state as soon as any reload succeeds -- the tab's Refresh or the
    // stream's background reconnect, neither of which goes through loadAgent.
    // Reading the phase (not just the error) is what keeps those two from
    // falling through to "No events yet for this agent." while they are in
    // flight, which is a lie the panel used to tell for the whole of a retry.
    const load = getConversationLoadState(agentId);
    if (hasNothingToShow && load.phase === "loading") {
      return m(
        "div",
        { class: "message-list-loading flex items-center justify-center h-full" },
        m("p", { class: "text-text-secondary" }, "Loading events..."),
      );
    }

    if (hasNothingToShow && load.error !== null) {
      return m("div", { class: "message-list-error flex flex-col items-center justify-center h-full gap-3" }, [
        m("p", { class: "text-red-500" }, `Error: ${load.error}`),
        m("button", { class: RELOAD_BUTTON_CLASS, onclick: () => reloadAfterFailure(agentId) }, "Refresh"),
      ]);
    }

    // The same failure, over a transcript that is already on screen. Keeping the
    // transcript is right -- blanking it loses more than the error tells -- but
    // staying silent is not: the user may have just asked for this reload
    // themselves, and got no answer either way. So it reports as a strip above the
    // transcript rather than in place of it, carrying the same retry the error
    // screen offers.
    const failedReloadNotice =
      load.error === null
        ? null
        : m("div", { class: "message-list-stale-notice flex items-center gap-3 border-b border-border px-3 py-1.5" }, [
            m("span", { class: "text-sm text-red-500" }, `Couldn't refresh this conversation: ${load.error}`),
            m("button", { class: RELOAD_BUTTON_CLASS, onclick: () => reloadAfterFailure(agentId) }, "Refresh"),
          ]);

    const events = getEventsForAgent(agentId);

    if (events.length === 0) {
      // No transcript yet -- but render any queued or in-flight message rather
      // than the empty-state placeholder (see tailNodes above).
      if (tailNodes.length === 0) {
        return m(
          "div",
          { class: "message-list-empty flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "No events yet for this agent."),
        );
      }
      return m("div", { class: "message-list-wrapper" }, [
        failedReloadNotice,
        m("div", { class: MESSAGE_LIST_CLASS }, tailNodes),
      ]);
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
      cachedRows = buildConversationRows(agentId, events, agentIsIdle);
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;

    // The engine decides everything about what mounts: the virtual end spacers,
    // the visible row window (viewport + overscan, grown while a selection is
    // live), and -- in afterRender -- where the viewport sits. Rendered as
    // spacer / row-run / spacer via the shared segment renderer.
    const plan = engine.computeRenderPlan();
    return m("div", { class: "message-list-wrapper" }, [
      failedReloadNotice,
      // The queued-message group renders after the virtualized rows so it sits at
      // the live tail, below the last committed turn. It is a full snapshot from
      // the harness, replaced wholesale on each push.
      m("div", { class: MESSAGE_LIST_CLASS }, [
        ...renderTranscriptSegments(rows, [
          { kind: "spacer", height: plan.topPadPx },
          { kind: "rows", startIndex: plan.startIndex, endIndex: plan.endIndex },
          { kind: "spacer", height: plan.bottomPadPx },
        ]),
        ...renderQueuedMessages(agentId),
        ...renderOutgoingMessages(agentId),
      ]),
    ]);
  }

  const handleAgentsUpdated = (): void => retryAfterAgentResolved();

  const handleMessageSent = (agentId: string): void => {
    if (agentId === currentAgentId) {
      engine.noteMessageSent();
    }
  };

  return {
    oninit() {
      addAgentsUpdatedListener(handleAgentsUpdated);
      addMessageSentListener(handleMessageSent);
    },

    onremove() {
      removeAgentsUpdatedListener(handleAgentsUpdated);
      removeMessageSentListener(handleMessageSent);
      disconnectLogWs();
      engine.detach();
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

      const content = isSlotClaimed("conversation-content") ? null : renderMessages(agentId);

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
          // The transcript area: the scroll container (native scrolling, native
          // scrollbar hidden), the custom overlay scrollbar, and the
          // loading-overlay for when the viewport sits over a virtual end spacer.
          m("div", { class: "chat-transcript-area relative flex-1 min-h-0 flex flex-col" }, [
            m(
              "main",
              {
                class: "app-content transcript-scroll flex-1 overflow-y-auto px-8 py-6",
                // Focusable so native keyboard scrolling (PageUp/Down, Home/End)
                // works; the engine's listeners classify the input source.
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
            // While the viewport is over a virtual end spacer (e.g. the scrollbar
            // was dragged into not-yet-loaded history), overlay a loading indicator
            // so the user never sees a blank area. pointer-events:none so it never
            // blocks scroll.
            engine.isViewportInSpacer()
              ? m(
                  "div",
                  {
                    class:
                      "message-list-window-loading absolute inset-0 flex items-center justify-center p-6 pointer-events-none",
                  },
                  m("p", { class: "text-text-secondary" }, "Loading messages..."),
                )
              : null,
          ]),
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
