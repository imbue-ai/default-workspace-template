/**
 * Single shared dockview workspace. All agents, chats, terminals, and
 * apps coexist as tabs in one DockviewComponent.
 */

import m from "mithril";
import {
  DockviewComponent,
  Orientation,
  themeLight,
  type DockviewGroupPanel,
  type GroupviewPanelState,
  type IContentRenderer,
  type IHeaderActionsRenderer,
  type SerializedDockview,
} from "dockview-core";
import { ChatPanel } from "./ChatPanel";
import { AgentTerminalPanel } from "./AgentTerminalPanel";
import { IframePanel, IFRAME_PANEL_PANEL_ID_ATTR, reloadIframesForService } from "./IframePanel";
import { TerminalBanner } from "./TerminalBanner";
import { SubagentView } from "./SubagentView";
import { CreateAgentModal } from "./CreateAgentModal";
import { CreateBrowserModal } from "./CreateBrowserModal";
import { DestroyConfirmDialog } from "./DestroyConfirmDialog";
import { ShareModal } from "./ShareModal";
import type { QuickAddTabType } from "./Sidebar";
import { effectiveLifecycleState, livenessCategoryForState } from "./agentLiveness";
import { attachHoverTooltip } from "./hoverTooltip";
import {
  addActivityOverlayListener,
  getEffectiveActivityState,
  removeActivityOverlayListener,
} from "../models/PendingMessages";
import { CLOSE_ACTIVE_TAB } from "@minds/embed-contract";
import { setEmbedderMessageHandler } from "../embed";
import { reloadInterface } from "../reload";
import { reportActivity } from "../models/activityReporter";
import { icon } from "./icons";
import type { IconName } from "./icons";
import { apiUrl, getPrimaryAgentId } from "../base-path";
import { deriveServiceOrigin } from "../origin";
import {
  addAgentsUpdatedListener,
  addLayoutOpListener,
  addProjectSyncListener,
  addTerminalSessionListener,
  allocateTerminalName,
  buildSessionTerminalUrl,
  fetchTerminalSessions,
  getAgentById,
  getAgents,
  getApps,
  getProtoAgents,
  labelForService,
  removeAgentLocally,
  removeAgentsUpdatedListener,
  reportClientState,
  type AgentsUpdatedListener,
  type AppEntry,
  type LayoutOpEvent,
  type LayoutOpListener,
  type ProjectSyncEvent,
  type ProjectSyncListener,
  type TerminalSessionInfo,
  type TerminalSessionListener,
} from "../models/AgentManager";
import {
  getActiveProjectId,
  getClientId,
  getStoredProjectId,
  setActiveLayoutSlug,
  setActiveProjectId,
} from "../models/ClientIdentity";
import { loadSnapshotWithStream } from "../models/StreamingMessage";
import {
  autosaveProject,
  chooseInitialProject,
  fetchProjectContent,
  fetchProjectsList,
  removePanelFromAllProjects,
  EVERYTHING_PROJECT_ID,
  type ProjectInfo,
} from "../models/Projects";

const AUTOSAVE_DEBOUNCE_MS = 1500;

// Panel-id prefixes for the two panel kinds whose ids encode their identity:
// a chat is ``chat-<agent-id>`` and a persistent terminal is
// ``terminal-session-<tmux-session-name>``. Deterministic ids are what let
// reopening the same chat / terminal focus the existing tab rather than stack a
// duplicate -- and what lets ``derivePanelParamsFromId`` rebuild a panel's
// params from its id alone when the bookkeeping entry is missing.
const CHAT_PANEL_ID_PREFIX = "chat-";
const TERMINAL_PANEL_ID_PREFIX = "terminal-session-";

/** Split the body of a ``service:`` ref into its service name and an
 *  optional ``?query`` suffix. Plain ``service:web`` yields
 *  ``{name: "web", query: ""}``; ``service:browser?session=2`` yields
 *  ``{name: "browser", query: "?session=2"}``. The browser fleet is the one
 *  case that uses the query: each browser pane is addressed as
 *  ``service:browser?session=<id>`` so distinct sessions resolve to distinct
 *  panels. The query is preserved verbatim so the resolved iframe URL and
 *  the dedup key both include it. */
function parseServiceRefBody(body: string): { name: string; query: string } {
  const queryIndex = body.indexOf("?");
  if (queryIndex === -1) return { name: body, query: "" };
  return { name: body.substring(0, queryIndex), query: body.substring(queryIndex) };
}

/** Resolve a ``service:`` ref body to its iframe URL. The query (e.g.
 *  ``?session=2``) is appended after the service's derived origin so a
 *  browser-session ref resolves to ``http://browser.<ws-host>/?session=2`` --
 *  the viewer's per-session entrypoint. Plain refs resolve to the bare
 *  service origin. */
function serviceRefUrl(body: string): string {
  const { name, query } = parseServiceRefBody(body);
  return `${deriveServiceOrigin(labelForService(name))}${query}`;
}

/** The ``?query`` suffix of a stored iframe URL, or "" when it has none.
 *  Used on layout restore to carry a persisted URL's query (e.g. a browser
 *  pane's ``session=<name>``) onto a freshly derived service origin. */
function urlQuerySuffix(url: string | undefined): string {
  if (!url) return "";
  const queryIndex = url.indexOf("?");
  return queryIndex === -1 ? "" : url.substring(queryIndex);
}

/** Extract the ``session`` id from a service ref ``?query`` for use in a tab
 *  title (``?session=2`` -> ``"2"``). Falls back to the raw query (minus the
 *  leading ``?``) when there is no ``session`` param. */
function serviceSessionLabel(query: string): string {
  const params = new URLSearchParams(query.startsWith("?") ? query.substring(1) : query);
  return params.get("session") ?? query.replace(/^\?/, "");
}

export function getTerminalUrl(): string {
  return deriveServiceOrigin(labelForService("terminal"));
}

/** Build the iframe URL that attaches a terminal to ``agentName``'s tmux
 *  session. The ttyd dispatch reads ``$1`` ("_") then ``$2`` ("agent")
 *  then ``$3`` (the agent name), so the args are written in that order.
 *  Used by the chat panel's "Open agent terminal" button and the
 *  agent-driven ``chat-terminal:<name>`` ref so both surfaces agree on
 *  the canonical URL (which the server's ``_extract_agent_terminal_name``
 *  parses back out when building refs from persisted layout state). */
export function buildAgentTerminalUrl(agentName: string): string {
  const baseUrl = getTerminalUrl();
  const separator = baseUrl.includes("?") ? "&" : "?";
  return `${baseUrl}${separator}arg=_&arg=agent&arg=${encodeURIComponent(agentName)}`;
}

/** Rebuild a restored agent-terminal URL on the current host, or null when
 *  ``url`` is not an agent-terminal URL. Agent terminals are identified by
 *  their ttyd dispatch args (``arg=_&arg=agent[&arg=<name>]``) -- the shape
 *  ``buildAgentTerminalUrl`` emits and no other iframe URL uses -- with the
 *  agent name riding in the third arg. Persisted URLs are absolute origins
 *  and therefore stale hints from whichever host saved the layout, so the
 *  restore path re-derives them here to keep layouts portable. */
function rebuildAgentTerminalUrl(url: string): string | null {
  const args = new URLSearchParams(urlQuerySuffix(url).replace(/^\?/, "")).getAll("arg");
  if (args[0] !== "_" || args[1] !== "agent") return null;
  // The name-less variant is the ChatPanel fallback for an agent that isn't
  // in the local cache yet; rebuild it without a name too.
  return args.length >= 3 ? buildAgentTerminalUrl(args[2]) : `${getTerminalUrl()}?arg=_&arg=agent`;
}

type PanelType = "chat" | "iframe" | "subagent";

interface PanelParams {
  panelType: PanelType;
  agentId: string;
  chatAgentId?: string;
  url?: string;
  title?: string;
  subagentSessionId?: string;
  // Workspace service name this iframe is tied to (e.g. "web", "api").
  // Set only for iframe tabs that proxy an actual workspace service; left
  // undefined for ad-hoc URL tabs, terminals, and agent-owned iframes.
  // Drives both the WS-driven `layout_op` (op="refresh") service-wide
  // reload match and the presence of the per-tab Refresh button.
  serviceName?: string;
  // Set only on persistent-terminal iframe tabs. ``terminalSessionName`` is
  // the named tmux session the tab attaches to (attach-or-create); its
  // presence is what marks a panel as a terminal (drives the banner, the
  // Destroy button, and layout-restore reattach). ``terminalId`` is a
  // per-tab id passed into the ttyd URL so the backend can map this tab's
  // tmux client back to us for live title tracking. ``terminalSessionId`` is
  // the immutable ``#{session_id}`` used to reflect a rename onto the tab.
  terminalSessionName?: string;
  terminalId?: string;
  terminalSessionId?: string;
}

// Modal state
let showNewChatModal = false;
let showNewAgentModal = false;
let showNewBrowserModal = false;
// When a background create POST fails, the New-browser modal is re-opened
// pre-filled with the name the user typed and the daemon's reason, so the user
// always learns WHY the browser didn't open (rather than the optimistic pane
// silently vanishing). Both are cleared on a clean open / cancel.
let newBrowserPrefillName: string | null = null;
let newBrowserError: string | null = null;

// The dockview group whose header "+" button opened the New chat / New agent
// modal. Captured at click time because those modals create their chat panel
// asynchronously (after the user confirms), by which point the active group
// may have changed. Consumed in the modal's onCreated callback so the new
// chat lands in the split the "+" was clicked in, then cleared. Null when the
// flow was started from the empty-state overlay (no host group).
let newTabTargetGroup: DockviewGroupPanel | null = null;

// Second paragraph of each destroy confirmation. Destroying is not just a
// louder Close: Close drops the tab from the project on screen, while Destroy
// takes it out of every project (its panel is what makes it a member of one),
// so every variant says that outright. The chat variant also says the
// transcript survives, because that is the thing people are actually afraid of
// losing.
const DESTROY_CHAT_DETAILS =
  "The tab is removed from every project, not just this one. The transcript stays accessible.";
const DESTROY_TERMINAL_DETAILS =
  "The tmux session is killed and the tab is removed from every project, not just this one.";
const DESTROY_BROWSER_DETAILS =
  "The browser is retired from the fleet and the tab is removed from every project, not just this one.";
const DESTROY_PANEL_DETAILS = "The tab is removed from every project, not just this one. Nothing else is torn down.";

// Destroy dialog state
let showDestroyDialog = false;
let destroyTargetAgentId: string | null = null;
let destroyTargetAgentName: string | null = null;
let destroyTargetPanelId: string | null = null;

// Terminal-destroy dialog state. Separate from the agent-destroy dialog above
// because destroying a terminal kills its tmux session (via the terminals API)
// rather than an mngr agent.
let showTerminalDestroyDialog = false;
let terminalDestroySessionName: string | null = null;
let terminalDestroyPanelId: string | null = null;

// Browser-destroy dialog state. Separate again because destroying a browser
// retires it in the fleet (DELETE /api/browsers/<name>, a same-origin passthrough
// to the browser daemon) rather than killing an mngr agent or a tmux session.
let showBrowserDestroyDialog = false;
let browserDestroyName: string | null = null;
let browserDestroyPanelId: string | null = null;

// Panel-destroy dialog state, for the tab kinds that have no separate resource
// behind them (apps, ad-hoc URL tabs, subagent views). Destroying one of those
// is exactly "take this tab out of every project", which is what distinguishes
// it from Close (which only removes it from the project you are looking at).
let showPanelDestroyDialog = false;
let panelDestroyTitle: string | null = null;
let panelDestroyPanelId: string | null = null;

// Share modal state
let showShareModal = false;
let shareServiceName: string | null = null;

interface SavedLayout {
  dockview: SerializedDockview;
  panelParams: Record<string, PanelParams>;
}

// Single shared dockview state
let dockview: DockviewComponent | null = null;
let dockviewContainer: HTMLElement | null = null;
const panelParams = new Map<string, PanelParams>();
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let _layoutOpListener: LayoutOpListener | null = null;
let _projectSyncListener: ProjectSyncListener | null = null;
let _terminalSessionListener: TerminalSessionListener | null = null;
let initialized = false;

// ---------- Active-project state ----------

// Cached project registry, used for the display names in the
// project-was-deleted notice; refreshed at startup and on every project
// broadcast.
let availableProjects: ProjectInfo[] = [];
// The project whose content is currently mounted in the dockview, and so also
// the project autosave writes to. Deliberately distinct from the *chosen*
// project in ClientIdentity, which the picker persists the moment the user
// clicks -- before this module has loaded anything: saving against the chosen
// id would write the outgoing project's arrangement into the incoming one, and
// the already-on-it guard in ``switchToProject`` would read a pick that
// recorded its choice first as a no-op and never load it.
let mountedProjectId: string | null = null;
// Serialized form of the layout content last persisted to (or received
// from) the server for the active project. Autosave skips the POST when the
// current serialization matches -- the content guard half of the live-sync
// echo suppression.
let lastPersistedLayoutJson: string | null = null;
// Autosaves are suppressed until this timestamp while a remotely-received
// layout is being applied: applying content fires onDidLayoutChange (and
// post-apply resize events), and persisting/broadcasting those re-applies
// would ping-pong saves between clients whose window sizes differ. The
// window comfortably covers the debounce plus the resize settle.
let suppressAutosaveUntilMs = 0;

const REMOTE_APPLY_SUPPRESS_MS = AUTOSAVE_DEBOUNCE_MS * 2 + 1000;

// Target fraction of horizontal space that the newly-opened service panel
// takes when it splits alongside the primary agent's chat. Picked so the
// just-built view dominates while the chat stays legible.
const OPEN_TAB_SPLIT_FRACTION = 0.6;

function createMithrilRenderer(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  component: m.ComponentTypes<any, any>,
  attrs: Record<string, unknown>,
): IContentRenderer {
  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  element.style.display = "flex";
  element.style.flexDirection = "column";

  // dockview keeps inactive tabs mounted (defaultRenderer: "always"), and
  // mithril's m.redraw() is global, so a hidden panel's component keeps
  // redrawing while its element is collapsed to zero size. Thread dockview's
  // authoritative panel-visibility signal into the component (as the
  // ``isVisible`` attr) so it can skip work that must not run while hidden --
  // e.g. ChatPanel's scroll management, which would otherwise corrupt the
  // retained scroll position against the zero-sized element. Defaults to true
  // so a component mounted without a panel api behaves as before.
  let panelVisible = true;
  let visibilityDisposable: { dispose: () => void } | null = null;

  return {
    element,
    init(parameters) {
      panelVisible = parameters.api.isVisible;
      visibilityDisposable = parameters.api.onDidVisibilityChange((event) => {
        panelVisible = event.isVisible;
        // Redraw so the component re-runs its lifecycle hooks with the new
        // visibility -- in particular so ChatPanel restores its scroll position
        // on the first redraw after the tab is shown again.
        m.redraw();
        // A tab switch changes which chat is visible; report so the OOM
        // prioritizer re-scores (a visible chat is more protected).
        reportChatTabActivity();
      });
      m.mount(element, { view: () => m(component, { ...attrs, isVisible: panelVisible }) });
    },
    dispose() {
      if (visibilityDisposable !== null) {
        visibilityDisposable.dispose();
        visibilityDisposable = null;
      }
      m.mount(element, null);
    },
  };
}

function createTabActionButton(
  title: string,
  iconName: IconName,
  onClick: (ev: MouseEvent) => void,
  className: string = "",
): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.className = `dv-custom-tab-action ${className}`.trim();
  btn.title = title;
  // No explicit size: `.dv-custom-tab-action svg` sizes these to 12px in CSS.
  btn.innerHTML = icon(iconName);
  btn.addEventListener("pointerdown", (ev) => ev.preventDefault());
  btn.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    onClick(ev);
  });
  return btn;
}

interface TabActionMenuItem {
  label: string;
  action: () => void;
}

/**
 * A tab action that opens a small menu instead of firing straight away.
 *
 * The menu is appended to ``document.body`` and positioned (fixed) under the
 * button rather than nested inside the tab: the tab strip clips its own
 * overflow (``.dv-tabs-container`` is ``overflow: auto``), so an in-tab menu
 * would be cut off -- the same constraint that puts the liveness tooltip on the
 * body (see ``attachHoverTooltip``). Items are built on each open so a menu
 * reflects the panel's current params.
 */
function createTabActionMenuButton(
  title: string,
  iconName: IconName,
  buildItems: () => TabActionMenuItem[],
): { button: HTMLButtonElement; dispose: () => void } {
  let menu: HTMLElement | null = null;

  const closeMenu = (): void => {
    menu?.remove();
    menu = null;
  };

  const button = createTabActionButton(title, iconName, () => {
    if (menu !== null) {
      closeMenu();
      return;
    }
    const element = document.createElement("div");
    element.className = "dockview-add-tab-dropdown";
    const rect = button.getBoundingClientRect();
    element.style.position = "fixed";
    element.style.top = `${rect.bottom + 4}px`;
    element.style.left = `${rect.left}px`;
    // The class positions itself against a tab-local anchor; this menu is
    // body-level, so the anchoring `right` has to go.
    element.style.right = "auto";
    for (const item of buildItems()) {
      const itemElement = document.createElement("div");
      itemElement.className = "dockview-add-tab-dropdown-item";
      itemElement.textContent = item.label;
      itemElement.addEventListener("click", (event) => {
        event.stopPropagation();
        closeMenu();
        item.action();
      });
      element.appendChild(itemElement);
    }
    document.body.appendChild(element);
    menu = element;
  });

  // The button's own click stops propagation, so this only ever fires for
  // clicks elsewhere -- i.e. it is the click-away close.
  const closeOnOutsideClick = (): void => {
    closeMenu();
  };
  document.addEventListener("click", closeOnOutsideClick);

  return {
    button,
    dispose(): void {
      document.removeEventListener("click", closeOnOutsideClick);
      closeMenu();
    },
  };
}

/** Reload the single iframe rendered for ``panelId``.
 *
 *  Looks the element up by its panel-id attribute and triggers a same-origin
 *  ``contentWindow.location.reload()``. A cross-origin panel throws a
 *  SecurityError on that call, so the fallback re-assigns ``src`` to force the
 *  browser to refetch. Shared by the per-tab Refresh button and the
 *  agent-driven ``refresh`` op. */
function reloadIframeForPanel(panelId: string): void {
  const iframe = document.querySelector<HTMLIFrameElement>(
    `iframe[${IFRAME_PANEL_PANEL_ID_ATTR}="${CSS.escape(panelId)}"]`,
  );
  if (!iframe) return;
  try {
    const win = iframe.contentWindow;
    if (win !== null) {
      win.location.reload();
      return;
    }
  } catch {
    // Cross-origin: fall through to src reassignment.
  }
  const currentSrc = iframe.getAttribute("src");
  if (currentSrc !== null) iframe.setAttribute("src", currentSrc);
}

/** Reload whatever a tab is showing, whichever kind of tab it is.
 *
 *  A service-backed iframe reloads service-wide (every pane on that service,
 *  which is what an app's own Refresh has always meant); any other iframe
 *  reloads just itself; a chat refetches its transcript snapshot and
 *  reconnects its stream. A subagent view owns its own stream and exposes no
 *  refetch, so it is re-rendered. */
function refreshPanelContent(panelId: string): void {
  const params = panelParams.get(panelId);
  if (params === undefined) return;
  if (params.panelType === "chat") {
    const chatAgentId = params.chatAgentId ?? params.agentId;
    void loadSnapshotWithStream(chatAgentId)
      .catch(() => {
        // The transcript that was already on screen stays; the chat's own
        // reconnect loop keeps retrying.
      })
      .finally(() => {
        m.redraw();
      });
    return;
  }
  if (params.panelType === "subagent") {
    m.redraw();
    return;
  }
  if (params.serviceName) {
    reloadIframesForService(params.serviceName);
    return;
  }
  reloadIframeForPanel(panelId);
}

/** The URL a tab can be opened in its own browser window at, or null when it
 *  has none. Only iframe tabs have a standalone URL -- a chat or a subagent
 *  view exists solely inside this workspace. */
function panelWindowUrl(panelId: string): string | null {
  const params = panelParams.get(panelId);
  if (params === undefined || params.panelType !== "iframe") return null;
  return params.url ? params.url : null;
}

/** The name the Share modal talks about for a tab: its service where it has
 *  one, else its title. */
function shareNameForPanel(panelId: string, tabTitle: string): string {
  const params = panelParams.get(panelId);
  return params?.serviceName ?? params?.title ?? tabTitle ?? "web";
}

function createCustomTab(options: { id: string; name: string }): {
  element: HTMLElement;
  init: (params: {
    title: string;
    api: {
      close: () => void;
      onDidTitleChange: (cb: (e: { title: string }) => void) => { dispose: () => void };
      isActive: boolean;
      onDidActiveChange: (cb: (e: { isActive: boolean }) => void) => { dispose: () => void };
    };
  }) => void;
  dispose: () => void;
} {
  const element = document.createElement("div");
  element.className = "dv-default-tab dv-custom-tab";

  const content = document.createElement("div");
  content.className = "dv-default-tab-content";
  element.appendChild(content);

  const actions = document.createElement("div");
  actions.className = "dv-custom-tab-actions";
  actions.style.display = "none";
  element.appendChild(actions);

  const disposables: Array<{ dispose: () => void }> = [];

  return {
    element,
    init(params) {
      content.textContent = params.title ?? "";
      disposables.push(
        params.api.onDidTitleChange((event) => {
          content.textContent = event.title ?? "";
        }),
      );

      const pp = panelParams.get(options.id);
      const panelType = pp?.panelType ?? "chat";
      // Terminal tabs are iframe panels, but destroying one kills its tmux
      // session rather than an agent, so they need telling apart.
      const isTerminal = isTerminalPanelParams(pp);

      // Every tab type carries the same actions, in the same order: Refresh,
      // Open in new window, Share, Destroy, Close. What each one *does* varies
      // by tab type (see the helpers below), but where the user has to reach
      // for it does not.

      actions.appendChild(
        createTabActionButton("Refresh", "refresh", () => {
          refreshPanelContent(options.id);
        }),
      );

      // Only an iframe tab has a URL that stands on its own; a chat or a
      // subagent view exists solely inside this workspace, so the button is
      // present but disabled there (same treatment as the primary agent's
      // Destroy). The URL is read at click time so an agent-driven
      // ``replace-url`` -- or a terminal whose session name is still being
      // allocated -- opens what the tab is showing now.
      const canOpenInWindow = panelType === "iframe";
      const openWindowButton = createTabActionButton(
        canOpenInWindow ? "Open in new window" : "This tab has no address of its own",
        "external-link",
        () => {
          const url = panelWindowUrl(options.id);
          if (url === null) return;
          window.open(url, "_blank", "noopener,noreferrer");
        },
        canOpenInWindow ? "" : "dv-custom-tab-action-disabled",
      );
      openWindowButton.disabled = !canOpenInWindow;
      actions.appendChild(openWindowButton);

      // Share is a menu so the sharing surface has room to grow; today it
      // holds the one action it has always had, which explains where sharing
      // is configured (ShareModal).
      const shareMenu = createTabActionMenuButton("Share", "share", () => [
        {
          label: "Share this tab...",
          action: () => {
            shareServiceName = shareNameForPanel(options.id, content.textContent ?? "");
            showShareModal = true;
            m.redraw();
          },
        },
      ]);
      actions.appendChild(shareMenu.button);
      disposables.push(shareMenu);

      // Destroy -- exactly one button, whose meaning follows the tab kind:
      // a chat destroys its agent, a terminal kills its tmux session, a
      // browser pane retires its browser, and anything else just leaves every
      // project. All four are confirm-gated, and all four take the tab out of
      // every project rather than only the one on screen (which is what Close
      // does).
      const openPanelDestroyDialog = (): void => {
        panelDestroyTitle = content.textContent ?? options.id;
        panelDestroyPanelId = options.id;
        showPanelDestroyDialog = true;
        m.redraw();
      };

      if (panelType === "chat") {
        const chatAgentId = pp?.chatAgentId ?? pp?.agentId ?? "";
        const primaryAgentId = getPrimaryAgentId();
        const isPrimary = chatAgentId === primaryAgentId;

        // Per-agent liveness dot, to the left of the title. Distinct from the
        // chat's activity indicator: this tracks the agent's mngr lifecycle
        // state -- green while its claude process is working (RUNNING), yellow
        // while it is idle and waiting on the user (WAITING), grey while it is
        // dormant (DONE/STOPPED/etc.; revives on the next message). Hovering
        // shows the exact lifecycle state via a body-level tooltip (a native
        // ``title`` is suppressed on dockview's draggable tabs -- see
        // ``attachHoverTooltip``). Hidden until the agent's state is known.
        //
        // The lifecycle RUNNING/WAITING split comes only from the backend's
        // lifecycle poll and lags a sent message, so the color is resolved through
        // ``effectiveLifecycleState`` against the prompt activity signal
        // (transcript-derived, plus the optimistic forced-THINKING the send
        // applies). That makes the dot turn green the instant a message is sent,
        // in step with the activity indicator -- hence the second listener below
        // on the activity overlay, since an optimistic send is not a WS update.
        const processDot = document.createElement("span");
        processDot.className = "dv-tab-process-dot";
        const processDotTooltip = attachHoverTooltip(processDot);
        const updateProcessDot = (): void => {
          const state = getAgentById(chatAgentId)?.state;
          if (!state) {
            processDot.style.display = "none";
            processDotTooltip.setText(null);
            return;
          }
          const effective = effectiveLifecycleState(state, getEffectiveActivityState(chatAgentId));
          processDot.style.display = "";
          // ``data-liveness`` drives the color (the primary signal). Several
          // lifecycle states share a color (DONE/STOPPED/REPLACED/UNKNOWN are
          // all grey "dormant"; RUNNING/RUNNING_UNKNOWN_AGENT_TYPE are both
          // green), so ``data-lifecycle-state`` carries the exact state and the
          // CSS gives each a subtly different circular treatment (solid / ring /
          // ring-with-dot / faded) so same-color states stay tellable apart.
          processDot.setAttribute("data-liveness", livenessCategoryForState(effective));
          processDot.setAttribute("data-lifecycle-state", effective);
          processDotTooltip.setText(effective);
        };
        updateProcessDot();
        element.insertBefore(processDot, element.firstChild);
        const processDotListener: AgentsUpdatedListener = () => updateProcessDot();
        addAgentsUpdatedListener(processDotListener);
        addActivityOverlayListener(updateProcessDot);
        disposables.push({ dispose: () => removeAgentsUpdatedListener(processDotListener) });
        disposables.push({ dispose: () => removeActivityOverlayListener(updateProcessDot) });
        disposables.push(processDotTooltip);

        const destroyBtn = createTabActionButton(
          isPrimary ? "Cannot destroy the primary agent" : "Destroy agent",
          "trash",
          () => {
            if (isPrimary) return;
            const agent = getAgentById(chatAgentId);
            destroyTargetAgentId = chatAgentId;
            destroyTargetAgentName = agent?.name ?? chatAgentId;
            destroyTargetPanelId = options.id;
            showDestroyDialog = true;
            m.redraw();
          },
          isPrimary ? "dv-custom-tab-action-disabled" : "dv-custom-tab-action-destructive",
        );
        if (isPrimary) {
          destroyBtn.disabled = true;
        }
        actions.appendChild(destroyBtn);
      } else if (isTerminal) {
        // Kills the tmux session (closing the tab alone only detaches). If the
        // session name isn't known yet (agent-driven terminal mid-allocation)
        // there is no session to kill, so it degrades to the plain
        // leave-every-project destroy.
        actions.appendChild(
          createTabActionButton(
            "Destroy terminal",
            "trash",
            () => {
              const sessionName = pp?.terminalSessionName;
              if (!sessionName) {
                openPanelDestroyDialog();
                return;
              }
              terminalDestroySessionName = sessionName;
              terminalDestroyPanelId = options.id;
              showTerminalDestroyDialog = true;
              m.redraw();
            },
            "dv-custom-tab-action-destructive",
          ),
        );
      } else if (panelType === "iframe" && pp?.serviceName === "browser") {
        // Retires the browser in the fleet (closing the tab alone only
        // detaches the pane). The browser name lives in the pane's
        // ``?session=<name>`` query, set on both freshly-opened and
        // layout-restored browser panes -- derive it from the url so both
        // cases work.
        let browserName: string | null = null;
        try {
          browserName = pp?.url ? new URL(pp.url, location.origin).searchParams.get("session") : null;
        } catch {
          browserName = null;
        }
        actions.appendChild(
          createTabActionButton(
            "Destroy browser",
            "trash",
            () => {
              if (!browserName) {
                openPanelDestroyDialog();
                return;
              }
              browserDestroyName = browserName;
              browserDestroyPanelId = options.id;
              showBrowserDestroyDialog = true;
              m.redraw();
            },
            "dv-custom-tab-action-destructive",
          ),
        );
      } else {
        // Apps, ad-hoc URL tabs, agent-owned iframes and subagent views: there
        // is no separate resource to tear down, so destroying is exactly
        // "remove this tab from every project".
        actions.appendChild(
          createTabActionButton("Destroy tab", "trash", openPanelDestroyDialog, "dv-custom-tab-action-destructive"),
        );
      }

      // Close button -- on all tab types
      actions.appendChild(
        createTabActionButton("Close tab", "close", () => {
          params.api.close();
        }),
      );

      // Show/hide actions based on active state
      function updateActionsVisibility(isActive: boolean): void {
        actions.style.display = isActive ? "flex" : "none";
      }
      updateActionsVisibility(params.api.isActive);
      disposables.push(
        params.api.onDidActiveChange((event) => {
          updateActionsVisibility(event.isActive);
        }),
      );
    },
    dispose() {
      for (const d of disposables) {
        d.dispose();
      }
      disposables.length = 0;
    },
  };
}

/** Report the current open/visible chat tabs to the backend (OOM priority).
 *
 *  Computed from ``dockview.panels`` (the live panel set) rather than
 *  ``panelParams`` so a just-removed panel isn't reported as still open when
 *  this fires from ``onDidLayoutChange`` before ``onDidRemovePanel`` clears its
 *  params. Only chat panels are reported; the report is debounced in the
 *  reporter, so calling it on every layout/visibility change is cheap. */
function reportChatTabActivity(): void {
  if (!dockview) return;
  const open: string[] = [];
  const visible: string[] = [];
  for (const panel of dockview.panels) {
    const pp = panelParams.get(panel.id);
    if (pp?.panelType !== "chat") continue;
    const chatId = pp.chatAgentId ?? pp.agentId;
    open.push(chatId);
    if (panel.api.isVisible) visible.push(chatId);
  }
  reportActivity({ open, visible });
}

/** Get the set of agent IDs that currently have open chat panels. */
function getOpenChatAgentIds(): Set<string> {
  const ids = new Set<string>();
  for (const [, pp] of panelParams) {
    if (pp.panelType === "chat") {
      ids.add(pp.chatAgentId ?? pp.agentId);
    }
  }
  return ids;
}

/** Get the set of app names that currently have open iframe panels. */
function getOpenAppNames(): Set<string> {
  const names = new Set<string>();
  for (const [, pp] of panelParams) {
    if (pp.panelType === "iframe" && pp.title) {
      names.add(pp.title);
    }
  }
  return names;
}

/** Placement options that tab a newly-added panel into ``targetGroup`` (the
 *  dockview group whose header "+" button was clicked) instead of letting
 *  dockview fall back to the currently-active group. Returns an empty object
 *  -- i.e. default placement -- when no target is given (the empty-state
 *  overlay has no host group) or the target group has since been disposed
 *  (e.g. it was closed while a New chat / New agent modal was open). */
function placementForGroup(
  targetGroup: DockviewGroupPanel | null | undefined,
): { position: { referenceGroup: DockviewGroupPanel } } | Record<string, never> {
  if (targetGroup && dockview?.groups.some((g) => g.id === targetGroup.id)) {
    return { position: { referenceGroup: targetGroup } };
  }
  return {};
}

// A single browser in the per-workspace fleet, as returned by the backend's
// same-origin ``GET /api/browsers`` passthrough. Each is a separately-
// addressable pane (viewer at ``?session=<name>`` on the browser service's
// derived origin). The ``id`` is the
// browser's NAME (a random ~2-word english name, or a user-chosen one) -- the
// addressing key everywhere; there is no numeric id and no default browser.
interface BrowserInfo {
  id: string;
  controller: "human" | "agent";
  owner_agent_id?: string | null;
  owner_name?: string | null;
  human_pinned?: boolean;
}

// Cached snapshot of the browser fleet, refreshed each time the "+" dropdown
// opens so it reflects browsers created since boot. The list drives one
// dropdown item per active browser.
//
// Note: we no longer gate the "New browser" button on the daemon's
// ``can_create``. A create is accepted even during startup/restore (it queues
// behind the serialized restore on the daemon's shared launch lock) and the
// fleet cap / duplicate-name rejections come back as inline errors in the
// New-browser modal, so the button stays always clickable.
let browserFleet: BrowserInfo[] = [];

/** Fetch the live browser fleet for the dropdown listing. ``onUpdate`` runs
 *  after the cache is refreshed so an already-open dropdown can re-render with
 *  the browsers that the (async) fetch just returned -- the dropdown is built
 *  synchronously from the cache, so without this callback a freshly-opened
 *  menu would show a stale fleet until the next open. */
function refreshBrowserFleet(onUpdate?: () => void): void {
  // Same-origin backend passthrough (the browser service itself lives on a
  // sibling origin, so a direct fetch would be a cross-origin request).
  fetch(apiUrl("/api/browsers"))
    .then((r) => (r.ok ? r.json() : { browsers: [] }))
    .then((data) => {
      browserFleet = Array.isArray(data.browsers) ? (data.browsers as BrowserInfo[]) : [];
    })
    .catch(() => {
      browserFleet = [];
    })
    .finally(() => {
      onUpdate?.();
    });
}
// Seed the fleet cache at import time. Skipped in DOM-less (test) imports:
// ``apiUrl`` reads its base path from a <meta> tag, and every other
// import-time side effect in this module graph is likewise inert without a
// document. Interactive callers (the "+" dropdown, browser create) only run
// in a browser.
if (typeof document !== "undefined") {
  refreshBrowserFleet();
}

/** Human-readable owner suffix for a browser dropdown item:
 *  "(you took control)" when a human holds it, "(agent <name> has control)"
 *  when an agent does, or "" when it's free. */
function browserOwnerLabel(browser: BrowserInfo): string {
  if (browser.controller === "human") return browser.human_pinned ? " (you took control)" : " (free)";
  if (browser.controller === "agent") {
    const name = browser.owner_name ?? browser.owner_agent_id ?? "agent";
    return ` (agent ${name} has control)`;
  }
  return "";
}

/** Open (or focus, via ``addPanelForRef`` dedup) the pane for browser
 *  ``name``. Routed through the same ``service:browser?session=<name>`` ref the
 *  agent CLI uses so the two surfaces share dedup/focus and on-disk shape.
 *  If the pane is already open, ``addPanelForRef`` focuses it; opening a new
 *  pane activates it (the user explicitly asked for this browser from the
 *  "+" menu, so taking focus is the intended behavior, matching every other
 *  "+" menu action). Tabs into ``targetGroup`` when it's a live group.
 *
 *  This is also the optimistic 'starting' pane: when called right after the
 *  user accepts a name in the New-browser modal (before the launch finishes),
 *  the viewer shows "Browser starting…" and retries the cast connection until
 *  the daemon registers the name.
 *
 *  Returns ``true`` when this call CREATED a new pane, ``false`` when it merely
 *  deduped onto (focused) a pane that was already open for the same browser.
 *  The optimistic-create flow uses this to decide whether a later failure may
 *  close the pane: it must only tear down a pane THIS flow created, never one
 *  that was already showing a healthy, pre-existing browser. */
function openBrowserSessionTab(name: string, targetGroup?: DockviewGroupPanel | null): boolean {
  if (!dockview) return false;
  // Was a pane already open for this browser? If so, ``addPanelForRef`` will
  // dedup/focus it rather than create a new one -- report that to the caller.
  const alreadyOpen = findIframePanelIdForServiceRef(`browser?session=${name}`) !== null;
  const placement =
    targetGroup && dockview.groups.some((g) => g.id === targetGroup.id)
      ? { position: { referenceGroup: targetGroup.id } }
      : {};
  addPanelForRef(`service:browser?session=${name}`, getPrimaryAgentId(), placement);
  return !alreadyOpen;
}

/** Close the (optimistic) pane for browser ``name`` if it is open. Used when a
 *  create POST fails after the pane was opened on modal-accept: the launch
 *  never registered the name, so the pane would otherwise sit on a stale
 *  "Browser starting…" / "browser closed" banner forever. Dedup keys panes on
 *  the resolved ``service:browser?session=<name>`` URL, so the lookup mirrors
 *  ``openBrowserSessionTab``'s ref. */
function closeBrowserSessionTab(name: string): void {
  if (!dockview) return;
  const panelId = findIframePanelIdForServiceRef(`browser?session=${name}`);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) dockview.removePanel(panel);
}

function buildDropdownItems(
  targetGroup?: DockviewGroupPanel,
): Array<{ label: string; action: () => void; dividerAfter?: boolean; disabled?: boolean; disabledReason?: string }> {
  const items: Array<{
    label: string;
    action: () => void;
    dividerAfter?: boolean;
    disabled?: boolean;
    disabledReason?: string;
  }> = [];
  const openChatIds = getOpenChatAgentIds();
  const openAppNames = getOpenAppNames();

  // --- Existing items section ---

  // Apps that do not have open tabs. Exclude "system_interface"
  // (that's the surrounding chrome UI, not a tab-able app), "terminal"
  // (reachable via the "New terminal" menu item further down), and "browser"
  // (the fleet has its own per-session items + "New browser" below; the bare
  // browser app entry would open a session-less viewer that doesn't dedup
  // against the fleet panes).
  const apps = getApps().filter(
    (app) => app.name !== "system_interface" && app.name !== "terminal" && app.name !== "browser",
  );
  for (const app of apps) {
    if (!openAppNames.has(app.name)) {
      const serviceUrl = deriveServiceOrigin(labelForService(app.name));
      items.push({
        label: app.name,
        action: () => openIframeTab(serviceUrl, app.name, "iframe", app.name, targetGroup),
      });
    }
  }

  // Agents/chats that don't have open tabs
  const allAgents = getAgents();
  for (const agent of allAgents) {
    if (!openChatIds.has(agent.id)) {
      items.push({
        label: agent.name,
        action: () => addChatPanel(agent.id, agent.name, targetGroup),
      });
    }
  }

  // Proto-agents that don't have open tabs
  const protos = getProtoAgents();
  for (const proto of protos) {
    if (!openChatIds.has(proto.agent_id)) {
      items.push({
        label: `${proto.name} (creating...)`,
        action: () => addChatPanel(proto.agent_id, proto.name, targetGroup),
      });
    }
  }

  // Active browsers in the fleet -- one item each, labeled with the owner.
  // Clicking focuses the pane if it's already open (``openBrowserSessionTab``
  // dedups on the ``?session=<id>`` URL) or opens it otherwise. Built from
  // the cached fleet snapshot the dropdown open-handler refreshed.
  for (const browser of browserFleet) {
    items.push({
      label: `Browser ${browser.id}${browserOwnerLabel(browser)}`,
      action: () => openBrowserSessionTab(browser.id, targetGroup),
    });
  }

  // Live terminal sessions (any non-mngr- tmux session) that don't have an
  // open tab. Selecting one reattaches -- the session keeps running after its
  // tab is closed, so this is how a closed-but-alive terminal is reopened.
  const openTerminalNames = getOpenTerminalSessionNames();
  for (const terminal of terminalFleet) {
    if (!openTerminalNames.has(terminal.session_name)) {
      items.push({
        label: terminal.session_name,
        action: () => reattachTerminal(terminal.session_name, targetGroup),
      });
    }
  }

  // Add divider if we had existing items
  if (items.length > 0) {
    items[items.length - 1].dividerAfter = true;
  }

  // --- "New ..." items ---

  items.push({
    label: "New chat",
    action: () => {
      newTabTargetGroup = targetGroup ?? null;
      showNewChatModal = true;
      m.redraw();
    },
  });

  // New terminal -- allocates a fresh named tmux session anchored at the
  // primary agent's work_dir.
  items.push({
    label: "New terminal",
    action: () => {
      void openNewTerminal(targetGroup);
    },
  });

  // Direct control is keyless -- the agent (and you) drive the browser by hand.
  // The item stays ALWAYS clickable: a create is accepted even during
  // startup/restore (it queues behind the serialized restore on the daemon's
  // shared launch lock and just takes longer), so the button must not be
  // gated on init. Cap (3) and duplicate names are enforced server-side and
  // surfaced as inline errors in the New-browser modal. Clicking opens that
  // modal pre-filled with a random name, mirroring "New agent".
  items.push({
    label: "New browser",
    action: () => {
      newTabTargetGroup = targetGroup ?? null;
      // Clean open: drop any leftover failure pre-fill so the modal fetches a
      // fresh random name and shows no error.
      newBrowserPrefillName = null;
      newBrowserError = null;
      showNewBrowserModal = true;
      m.redraw();
    },
  });

  items.push({
    label: "New agent",
    action: () => {
      newTabTargetGroup = targetGroup ?? null;
      showNewAgentModal = true;
      m.redraw();
    },
  });

  return items;
}

/** Render ``buildDropdownItems(targetGroup)`` into ``dropdown`` (clearing it
 *  first). Shared by the header "+" button and the empty-state overlay so
 *  the item markup + click wiring live in one place. Re-invoked when the
 *  async browser-fleet fetch resolves so a freshly-opened menu picks up the
 *  live browser list. */
function renderDropdownItems(dropdown: HTMLElement, targetGroup?: DockviewGroupPanel): void {
  dropdown.innerHTML = "";
  const items = buildDropdownItems(targetGroup);
  for (const item of items) {
    const menuItem = document.createElement("div");
    menuItem.className = "dockview-add-tab-dropdown-item";
    menuItem.textContent = item.label;
    if (item.disabled) {
      menuItem.style.opacity = "0.5";
      menuItem.style.cursor = "not-allowed";
    }
    menuItem.addEventListener("click", (clickEvent) => {
      clickEvent.stopPropagation();
      dropdown.style.display = "none";
      // A disabled item doesn't run its action; if it has a reason, surface it (the
      // "click pops a modal explaining why" path). Without this a disabled item would
      // still fire -- only visually greyed.
      if (item.disabled) {
        if (item.disabledReason) alert(item.disabledReason);
        return;
      }
      item.action();
    });
    dropdown.appendChild(menuItem);

    if (item.dividerAfter) {
      const divider = document.createElement("div");
      divider.className = "dockview-add-tab-dropdown-divider";
      divider.style.borderTop = "1px solid #e5e7eb";
      divider.style.margin = "4px 0";
      dropdown.appendChild(divider);
    }
  }
}

function createAddTabButton(group: DockviewGroupPanel): IHeaderActionsRenderer {
  const element = document.createElement("div");
  element.className = "dockview-add-tab-wrapper";

  const button = document.createElement("button");
  button.className = "dockview-add-tab-button";
  button.title = "Add tab";
  button.textContent = "+";
  element.appendChild(button);

  const dropdown = document.createElement("div");
  dropdown.className = "dockview-add-tab-dropdown";
  dropdown.style.display = "none";

  element.appendChild(dropdown);

  button.addEventListener("click", (e) => {
    e.stopPropagation();
    const isVisible = dropdown.style.display !== "none";
    if (isVisible) {
      dropdown.style.display = "none";
    } else {
      // Build from current state, then refresh the browser fleet and
      // re-render once it resolves (only if the menu is still open) so the
      // active-browser items reflect the live fleet rather than a stale
      // snapshot from a previous open.
      renderDropdownItems(dropdown, group);
      refreshBrowserFleet(() => {
        if (dropdown.style.display !== "none") renderDropdownItems(dropdown, group);
      });
      refreshTerminalFleet(() => {
        if (dropdown.style.display !== "none") renderDropdownItems(dropdown, group);
      });
      dropdown.style.display = "block";
    }
  });

  const closeDropdown = (e: MouseEvent) => {
    if (!element.contains(e.target as Node)) {
      dropdown.style.display = "none";
    }
  };
  document.addEventListener("click", closeDropdown);

  return {
    element,
    init() {},
    dispose() {
      document.removeEventListener("click", closeDropdown);
    },
  };
}

function focusOrCreateChatPanel(
  chatAgentId: string,
  chatAgentName: string,
  targetGroup?: DockviewGroupPanel | null,
): void {
  if (!dockview) return;
  const panelId = chatPanelId(chatAgentId);
  const existingPanel = dockview.panels.find((p) => p.id === panelId);
  if (existingPanel) {
    if (!existingPanel.api.isActive) {
      dockview.setActivePanel(existingPanel);
    }
    return;
  }
  addChatPanel(chatAgentId, chatAgentName, targetGroup);
}

function addChatPanel(chatAgentId: string, chatAgentName: string, targetGroup?: DockviewGroupPanel | null): void {
  if (!dockview) return;
  const panelId = chatPanelId(chatAgentId);
  const params: PanelParams = { panelType: "chat", agentId: chatAgentId, chatAgentId };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "chat",
    title: chatAgentName,
    params,
    renderer: "always",
    ...placementForGroup(targetGroup),
  });
  appendPanelToEverythingProject(panelId);
}

/**
 * Open the workspace's initial (bootstrap-created) chat agent as the first
 * tab. "Initial" = the earliest non-is_primary agent we know about. In a
 * freshly-booted workspace the bootstrap creates exactly one chat agent
 * named after the host, and that's what we want here. The services agent
 * (is_primary=true) is filtered out by getAgents().
 *
 * If no non-is_primary agent exists yet (e.g. the workspace just started
 * and the bootstrap's `mngr create` is still running), returns false so
 * the caller can show a "waiting" state. We re-try when an agents_updated
 * event arrives.
 */
function openInitialChatTab(): boolean {
  const candidates = getAgents();
  if (candidates.length === 0) return false;
  const initial = candidates[0];
  addChatPanel(initial.id, initial.name);
  return true;
}

// `awaitingInitialChat` flips on when init runs against an empty agent
// list, and back off as soon as we successfully open the initial tab.
// While true, an agents_updated listener keeps retrying. The empty-state
// overlay uses this flag to decide whether to show "Waiting for initial
// chat agent..." vs the default "+ Open new tab" message.
let awaitingInitialChat = false;
let agentsUpdatedListener: AgentsUpdatedListener | null = null;

function openIframeTab(
  url: string,
  title: string,
  panelType: PanelType = "iframe",
  serviceName?: string,
  targetGroup?: DockviewGroupPanel | null,
): void {
  if (!dockview) return;
  const primaryId = getPrimaryAgentId();
  const panelId = `${panelType}-${primaryId}-${Date.now()}`;
  const params: PanelParams = { panelType, agentId: primaryId, url, title, serviceName };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "iframe",
    title,
    params,
    ...placementForGroup(targetGroup),
  });
  appendPanelToEverythingProject(panelId);
}

/** Find the chat panel id to anchor an agent-initiated split against.
 *
 *  Strict identity: the only acceptable anchor is the requester's own chat
 *  tab (``chat-<requesterAgentId>``). Returns null when the requester id is
 *  empty or their chat panel isn't open -- callers then either fall through
 *  to a non-chat-anchored placement (``handleOpenPanelRequest``) or no-op
 *  (``handleSplit`` / ``handleMove`` skip the relative_to=self branch). We
 *  intentionally do not auto-select another agent's chat: that would let
 *  ``layout.py split web`` from agent A land next to agent B's chat
 *  whenever A's chat happens not to be on screen, which is surprising. */
function findAnchorChatPanelId(requesterAgentId: string): string | null {
  if (!dockview) return null;
  if (!requesterAgentId) return null;
  const candidate = chatPanelId(requesterAgentId);
  return dockview.panels.find((p) => p.id === candidate) ? candidate : null;
}

/** Find an existing iframe panel for ``serviceName``, or null. */
function findIframePanelIdForService(serviceName: string): string | null {
  for (const [panelId, params] of panelParams) {
    if (params.panelType === "iframe" && params.serviceName === serviceName) {
      return panelId;
    }
  }
  return null;
}

/** Find an existing iframe panel for a ``service:`` ref body, or null.
 *
 *  Dedup is keyed on what makes the pane unique:
 *   - A ref with no query (``web``) dedups by ``serviceName`` -- the
 *     existing single-pane-per-service behavior.
 *   - A ref with a query (``browser?session=2``) dedups by the resolved
 *     URL, which embeds the query. Two browser panes with different
 *     ``?session=`` therefore resolve to different panels and never collide:
 *     opening ``service:browser?session=2`` focuses session 2's pane (or
 *     creates it) without touching session 0's. */
function findIframePanelIdForServiceRef(body: string): string | null {
  const { name, query } = parseServiceRefBody(body);
  if (query === "") {
    return findIframePanelIdForService(name);
  }
  return findIframePanelIdForUrl(serviceRefUrl(body));
}

/** Derive a tab title from an external URL: its hostname, falling back to
 *  the raw string when the URL can't be parsed. */
function externalUrlTitle(url: string): string {
  try {
    return new URL(url).hostname || url;
  } catch {
    return url;
  }
}

/** Find an existing iframe panel pointed at ``url``, or null. Used to
 *  dedup ad-hoc external-URL panels (focus-if-open instead of stacking
 *  duplicates), mirroring the service dedup in ``findIframePanelIdForService``. */
function findIframePanelIdForUrl(url: string): string | null {
  for (const [panelId, params] of panelParams) {
    if (params.panelType === "iframe" && params.url === url) {
      return panelId;
    }
  }
  return null;
}

/** Position + size options passed through to ``dockview.addPanel``. */
type AddPanelPlacementOptions = {
  position?: { referenceGroup: string } | { referencePanel: string; direction: "left" | "right" | "above" | "below" };
  initialWidth?: number;
  initialHeight?: number;
  /** Server-supplied panel id used verbatim for the new tab. Set only on
   *  agent-driven terminal creation (``open terminal`` / ``split terminal``):
   *  the broadcast endpoint pre-mints the id so its HTTP response can
   *  return the resulting ``terminal:<hash>`` ref synchronously. Ignored
   *  for every other ref kind. */
  panelIdHint?: string;
};

/** Deterministic dockview panel id for an agent's chat tab, so reopening the
 *  same chat focuses the existing tab instead of stacking a duplicate. */
function chatPanelId(chatAgentId: string): string {
  return `${CHAT_PANEL_ID_PREFIX}${chatAgentId}`;
}

/** Deterministic dockview panel id for a named terminal session, so reopening
 *  the same session from the "+" menu (or a layout restore) focuses the
 *  existing tab instead of stacking a duplicate. */
function terminalPanelId(sessionName: string): string {
  return `${TERMINAL_PANEL_ID_PREFIX}${sessionName}`;
}

/** Rebuild a panel's params from its (deterministic) panel id.
 *
 *  Chat and persistent-terminal panel ids encode their identity, so a panel
 *  whose ``panelParams`` entry is missing at creation time -- a layout file
 *  written by an older build, a hand-edited one, or a bookkeeping bug -- can
 *  still be bound to the right agent / tmux session instead of silently
 *  rendering someone else's (empty) transcript. Returns null for ids that
 *  carry no recoverable identity (ad-hoc URL / service iframes, subagents),
 *  whose params exist only in the map. */
function derivePanelParamsFromId(panelId: string): PanelParams | null {
  if (panelId.startsWith(CHAT_PANEL_ID_PREFIX)) {
    const chatAgentId = panelId.substring(CHAT_PANEL_ID_PREFIX.length);
    if (!chatAgentId) return null;
    return { panelType: "chat", agentId: chatAgentId, chatAgentId };
  }
  if (panelId.startsWith(TERMINAL_PANEL_ID_PREFIX)) {
    const sessionName = panelId.substring(TERMINAL_PANEL_ID_PREFIX.length);
    if (!sessionName) return null;
    const terminalId = mintTerminalId();
    return {
      panelType: "iframe",
      agentId: getPrimaryAgentId(),
      url: buildSessionTerminalUrl(sessionName, terminalId, primaryWorkDir()),
      title: sessionName,
      terminalSessionName: sessionName,
      terminalId,
    };
  }
  return null;
}

/** The params dockview should build a panel from: the ones it supplied, else
 *  the stored entry, else a re-derivation from the panel id.
 *
 *  A recovered entry is written back into ``panelParams``, so the next autosave
 *  also repairs the persisted layout. Returns null only when the panel's
 *  identity cannot be recovered at all -- the caller then renders an explicit
 *  placeholder rather than guessing an owner. */
function resolvePanelParams(panelId: string, suppliedParams: PanelParams | undefined): PanelParams | null {
  if (suppliedParams !== undefined) return suppliedParams;
  const stored = panelParams.get(panelId);
  if (stored !== undefined) return stored;
  const derived = derivePanelParamsFromId(panelId);
  if (derived === null) {
    console.warn(`Dockview panel ${panelId} has no params and none can be derived from its id`);
    return null;
  }
  console.warn(`Recovered missing params for dockview panel ${panelId} from its id`);
  panelParams.set(panelId, derived);
  return derived;
}

/** Content renderer for a panel whose params are missing and underivable. It
 *  says so plainly instead of rendering a plausible-looking wrong panel (e.g.
 *  the primary agent's empty transcript under another agent's tab title). */
function createUnrecoverablePanelRenderer(panelId: string): IContentRenderer {
  const element = document.createElement("div");
  element.className = "dockview-panel-unrecoverable";
  element.style.display = "flex";
  element.style.alignItems = "center";
  element.style.justifyContent = "center";
  element.style.height = "100%";
  element.style.padding = "16px";
  element.style.textAlign = "center";
  element.textContent = "This tab's contents could not be restored. Close it and open it again from the + menu.";
  console.warn(`Rendering unrecoverable-panel placeholder for dockview panel ${panelId}`);
  return {
    element,
    init() {},
    dispose() {},
  };
}

/** A panel is a persistent-terminal tab iff it carries terminal params.
 *  ``terminalId`` is set synchronously at creation (even before the tmux session
 *  name has been allocated), and ``terminalSessionName`` arrives with or after
 *  it, so either one marks a terminal panel. Single source of truth for the
 *  tab-action selection and the terminal-renderer choice. */
function isTerminalPanelParams(pp: PanelParams | undefined): boolean {
  return pp?.terminalSessionName !== undefined || pp?.terminalId !== undefined;
}

/** Mint a fresh per-tab terminal id. The backend maps this back to the tab's
 *  tmux client (via the pty) for live title tracking. */
function mintTerminalId(): string {
  const unique = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Date.now());
  return `term-${unique}`;
}

/** The primary agent's work_dir, or "" (the ttyd dispatch treats an empty
 *  work_dir as "start in $HOME"). New terminals anchor here. */
function primaryWorkDir(): string {
  return getAgentById(getPrimaryAgentId())?.work_dir ?? "";
}

// Cached snapshot of the live terminal-session fleet, refreshed each time the
// "+" dropdown opens (mirrors ``browserFleet``). Drives one dropdown item per
// non-open terminal session so a closed-but-alive terminal can be reattached.
let terminalFleet: TerminalSessionInfo[] = [];

function refreshTerminalFleet(onUpdate?: () => void): void {
  fetchTerminalSessions()
    .then((data) => {
      terminalFleet = data.terminals;
    })
    .finally(() => {
      onUpdate?.();
    });
}

/** Session names of terminals currently open in a tab, so the "+" menu can
 *  exclude them (mirrors ``getOpenAppNames`` for apps). */
function getOpenTerminalSessionNames(): Set<string> {
  const names = new Set<string>();
  for (const [, pp] of panelParams) {
    if (pp.terminalSessionName) names.add(pp.terminalSessionName);
  }
  return names;
}

/** Open (or focus, if already open) a tab attached to ``sessionName``. Shared
 *  by "New terminal" (freshly allocated name) and the "+" menu reattach path
 *  (existing name). ``panelIdOverride`` is used by the agent-driven
 *  ``service:terminal`` path so the server-minted panel id (and thus its
 *  ``terminal:<hash>`` ref) is preserved. */
function addTerminalPanel(
  sessionName: string,
  options: { panelId?: string; targetGroup?: DockviewGroupPanel | null },
): string | null {
  if (!dockview) return null;
  const panelId = options.panelId ?? terminalPanelId(sessionName);
  const existing = dockview.panels.find((p) => p.id === panelId);
  if (existing) {
    dockview.setActivePanel(existing);
    return panelId;
  }
  const terminalId = mintTerminalId();
  const url = buildSessionTerminalUrl(sessionName, terminalId, primaryWorkDir());
  const params: PanelParams = {
    panelType: "iframe",
    agentId: getPrimaryAgentId(),
    url,
    title: sessionName,
    terminalSessionName: sessionName,
    terminalId,
  };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "iframe",
    title: sessionName,
    params,
    ...placementForGroup(options.targetGroup),
  });
  appendPanelToEverythingProject(panelId);
  return panelId;
}

/** "New terminal" button: allocate the next free ``terminal-N`` name from the
 *  backend, then open a tab attached to it. */
async function openNewTerminal(targetGroup?: DockviewGroupPanel | null): Promise<void> {
  if (!dockview) return;
  let sessionName: string;
  try {
    sessionName = await allocateTerminalName();
  } catch (e) {
    // Allocation failed (backend unreachable); surface it rather than leaving
    // the "New terminal" click with no visible effect (matches the alert used
    // by the other terminal/agent actions in this file).
    alert(`Failed to open terminal: ${(e as Error).message}`);
    return;
  }
  addTerminalPanel(sessionName, { targetGroup });
}

/** "+" menu: reattach a tab to an already-running terminal session. */
function reattachTerminal(sessionName: string, targetGroup?: DockviewGroupPanel | null): void {
  addTerminalPanel(sessionName, { targetGroup });
}

/** Dedup-then-add for a ``service:``, ``chat:``, or ``https://`` ref.
 *
 *  Shared by ``handleSplit`` and ``handleOpenPanelRequest`` so that the
 *  panelParams bookkeeping + addPanel invocation only exist in one place.
 *  When a panel already exists for the ref (service: dedup by serviceName,
 *  except a ``service:browser?session=<id>`` browser-fleet ref which dedups
 *  by its ``?session=<id>`` URL so distinct sessions stay distinct panels;
 *  chat: dedup by deterministic ``chat-<agent-id>``, https:// dedup by
 *  URL), focuses it and returns its id. Otherwise creates the panel with
 *  the supplied positioning and returns the new id. A bare ``https://``
 *  ref creates an ad-hoc external-URL iframe tab. ``service:terminal`` is
 *  the one creation path that bypasses dedup: it mirrors the UI's "New
 *  terminal" button (each call adds a fresh tab) and uses
 *  ``addOptions.panelIdHint`` as the new panel id so the broadcast
 *  endpoint can return the resulting ``terminal:<hash>`` ref synchronously.
 *  Returns null when dockview isn't ready, the ref carries a prefix that
 *  doesn't create panels in v1 (subagent:/url:/bare ``terminal:``), or the
 *  named chat agent is unknown. */
function addPanelForRef(ref: string, requesterAgentId: string, addOptions: AddPanelPlacementOptions): string | null {
  if (!dockview) return null;
  // Strip ``panelIdHint`` from the addPanel spread: it's an
  // addPanelForRef-internal hint, not a dockview placement field.
  const { panelIdHint, ...placement } = addOptions;

  if (ref === "service:terminal") {
    const ownerId = requesterAgentId || getPrimaryAgentId();
    // Keep the server-minted panel id verbatim so the ``terminal:<hash>`` ref
    // the broadcast endpoint returned still resolves to this panel.
    const panelId = panelIdHint ?? `iframe-terminal-${Date.now()}`;
    const terminalId = mintTerminalId();
    // The tmux session name is allocated asynchronously; create the panel now
    // (so the ref resolves immediately) with a placeholder url and fill it in
    // once the backend hands back the next free ``terminal-N`` name. The
    // reactive terminal renderer reads ``params.url`` on each redraw, so
    // setting it after allocation swaps in the live session. ``terminalId``
    // is set synchronously, which is what marks this as a terminal panel for
    // the renderer + tab-action selection.
    const params: PanelParams = {
      panelType: "iframe",
      agentId: ownerId,
      url: "",
      title: "terminal",
      terminalId,
    };
    panelParams.set(panelId, params);
    dockview.addPanel({
      id: panelId,
      component: "iframe",
      title: "terminal",
      params,
      ...placement,
    });
    void allocateTerminalName()
      .then((sessionName) => {
        const stored = panelParams.get(panelId);
        if (!stored) return;
        stored.terminalSessionName = sessionName;
        stored.title = sessionName;
        stored.url = buildSessionTerminalUrl(sessionName, terminalId, primaryWorkDir());
        dockview?.panels.find((p) => p.id === panelId)?.api.setTitle(sessionName);
        m.redraw();
        scheduleSave();
        // Copied into Everything only once the session name has landed, so
        // the stored copy carries the live session rather than the
        // placeholder the panel was created with.
        appendPanelToEverythingProject(panelId);
      })
      .catch(() => {
        // Allocation failed: leave the placeholder tab so the user can close it.
      });
    return panelId;
  }

  if (ref.startsWith("service:")) {
    const body = ref.substring("service:".length);
    // Dedup distinguishes browser sessions: ``service:browser?session=2``
    // resolves to a different panel than ``service:browser?session=0`` (or
    // the bare ``service:browser``) because the query is part of the URL we
    // dedup on. Plain service refs still dedup by serviceName.
    const existingPanelId = findIframePanelIdForServiceRef(body);
    if (existingPanelId !== null) {
      const existing = dockview.panels.find((p) => p.id === existingPanelId);
      if (existing) dockview.setActivePanel(existing);
      return existingPanelId;
    }
    const { name: serviceName, query } = parseServiceRefBody(body);
    const ownerId = requesterAgentId || getPrimaryAgentId();
    const panelId = `iframe-${ownerId}-${Date.now()}`;
    // ``serviceName`` is the bare name (no query) so the per-tab Refresh
    // button and service-wide reload still match every browser pane. The
    // ``url`` carries the ``?session=`` query so the viewer selects the
    // right browser and so URL-based dedup keeps sessions distinct. The
    // title gets the session id appended (``browser?session=2`` ->
    // "browser 2") so multiple browser tabs are tellable apart.
    const url = serviceRefUrl(body);
    const title = query === "" ? serviceName : `${serviceName} ${serviceSessionLabel(query)}`;
    const params: PanelParams = {
      panelType: "iframe",
      agentId: ownerId,
      url,
      title,
      serviceName,
    };
    panelParams.set(panelId, params);
    dockview.addPanel({
      id: panelId,
      component: "iframe",
      title,
      params,
      ...placement,
    });
    appendPanelToEverythingProject(panelId);
    return panelId;
  }

  if (ref.startsWith("chat:")) {
    const agentName = ref.substring("chat:".length);
    const agent = getAgents().find((a) => a.name === agentName);
    if (!agent) return null;
    const panelId = chatPanelId(agent.id);
    const existing = dockview.panels.find((p) => p.id === panelId);
    if (existing) {
      dockview.setActivePanel(existing);
      return panelId;
    }
    const params: PanelParams = { panelType: "chat", agentId: agent.id, chatAgentId: agent.id };
    panelParams.set(panelId, params);
    dockview.addPanel({
      id: panelId,
      component: "chat",
      title: agent.name,
      params,
      renderer: "always",
      ...placement,
    });
    appendPanelToEverythingProject(panelId);
    return panelId;
  }

  if (ref.startsWith("chat-terminal:")) {
    // Per-agent terminal singleton: dedup by URL so opening the same ref
    // twice focuses the existing panel rather than stacking duplicates.
    // The URL is built by ``buildAgentTerminalUrl`` so the on-disk shape
    // matches what the server's ``_extract_agent_terminal_name`` projects
    // back to ``chat-terminal:<name>``.
    const agentName = ref.substring("chat-terminal:".length);
    const agent = getAgents().find((a) => a.name === agentName);
    if (!agent) return null;
    const url = buildAgentTerminalUrl(agentName);
    const existingPanelId = findIframePanelIdForUrl(url);
    if (existingPanelId !== null) {
      const existing = dockview.panels.find((p) => p.id === existingPanelId);
      if (existing) dockview.setActivePanel(existing);
      return existingPanelId;
    }
    const title = `${agentName} terminal`;
    // Owning agentId is the target agent (the terminal *is* that agent's),
    // not the requester. Matches the panel id format used by the chat
    // panel's "Open agent terminal" button so the two creation paths
    // produce identical bookkeeping.
    const panelId = `iframe-agent-${agent.id}-${Date.now()}`;
    const params: PanelParams = { panelType: "iframe", agentId: agent.id, url, title };
    panelParams.set(panelId, params);
    dockview.addPanel({
      id: panelId,
      component: "iframe",
      title,
      params,
      ...placement,
    });
    appendPanelToEverythingProject(panelId);
    return panelId;
  }

  if (ref.startsWith("https://")) {
    const existingPanelId = findIframePanelIdForUrl(ref);
    if (existingPanelId !== null) {
      const existing = dockview.panels.find((p) => p.id === existingPanelId);
      if (existing) dockview.setActivePanel(existing);
      return existingPanelId;
    }
    const ownerId = requesterAgentId || getPrimaryAgentId();
    const panelId = `iframe-${ownerId}-${Date.now()}`;
    const title = externalUrlTitle(ref);
    // ``serviceName`` is intentionally left unset: this is an ad-hoc URL
    // tab, not a proxied workspace service, so it is skipped by service-wide
    // reload matching and its per-tab Refresh reloads just this iframe.
    const params: PanelParams = { panelType: "iframe", agentId: ownerId, url: ref, title };
    panelParams.set(panelId, params);
    dockview.addPanel({
      id: panelId,
      component: "iframe",
      title,
      params,
      ...placement,
    });
    appendPanelToEverythingProject(panelId);
    return panelId;
  }

  return null;
}

/** Find a group adjacent to ``anchorGroupId`` in the requested direction.
 *
 *  Used by the "share existing splits" default for ``open`` / ``split`` /
 *  ``move``: if the caller asked to put a panel to the right of (say) a
 *  chat, and a service iframe is already living to the right of that
 *  chat, we'd rather tab the new panel into that existing group than
 *  jam another column between them.
 *
 *  Adjacency is measured geometrically off ``getBoundingClientRect`` --
 *  walking the persisted grid tree would also work but ties us to
 *  dockview-internal APIs that aren't part of its public surface.
 *  Among multiple candidates we pick the one with the largest overlap
 *  on the perpendicular axis: e.g. for ``direction: "right"`` we prefer
 *  the group whose vertical extent most closely tracks the anchor's.
 *  Returns null when no group lies in that direction. */
function findSiblingGroupInDirection(
  anchorGroupId: string,
  direction: "left" | "right" | "above" | "below",
): { id: string } | null {
  if (!dockview) return null;
  const anchor = dockview.groups.find((g) => g.id === anchorGroupId);
  if (!anchor) return null;
  const anchorRect = anchor.element.getBoundingClientRect();
  // Pixel slop: dockview separators round to whole pixels and adjacent
  // edges can be off-by-one after a resize.
  const tolerance = 2;
  let best: { id: string; overlap: number; distance: number } | null = null;
  for (const group of dockview.groups) {
    if (group.id === anchorGroupId) continue;
    const rect = group.element.getBoundingClientRect();
    let inDirection: boolean;
    let overlap: number;
    let distance: number;
    if (direction === "right") {
      inDirection = rect.left >= anchorRect.right - tolerance;
      overlap = Math.max(0, Math.min(rect.bottom, anchorRect.bottom) - Math.max(rect.top, anchorRect.top));
      distance = rect.left - anchorRect.right;
    } else if (direction === "left") {
      inDirection = rect.right <= anchorRect.left + tolerance;
      overlap = Math.max(0, Math.min(rect.bottom, anchorRect.bottom) - Math.max(rect.top, anchorRect.top));
      distance = anchorRect.left - rect.right;
    } else if (direction === "below") {
      inDirection = rect.top >= anchorRect.bottom - tolerance;
      overlap = Math.max(0, Math.min(rect.right, anchorRect.right) - Math.max(rect.left, anchorRect.left));
      distance = rect.top - anchorRect.bottom;
    } else {
      inDirection = rect.bottom <= anchorRect.top + tolerance;
      overlap = Math.max(0, Math.min(rect.right, anchorRect.right) - Math.max(rect.left, anchorRect.left));
      distance = anchorRect.top - rect.bottom;
    }
    if (!inDirection || overlap <= 0) continue;
    // Prefer larger perpendicular overlap; break ties by nearer distance.
    if (best === null || overlap > best.overlap || (overlap === best.overlap && distance < best.distance)) {
      best = { id: group.id, overlap, distance };
    }
  }
  return best === null ? null : { id: best.id };
}

/** Handle an agent-driven ``open`` broadcast for a creatable ``ref``
 *  (a ``service:`` ref or a bare ``https://`` external URL).
 *
 *  Resolution order:
 *    1. If a panel for ``ref`` is already open, focus it (handled by
 *       ``addPanelForRef``'s dedup).
 *    2. If the *requester's own* chat panel is open, add a right-split
 *       iframe sized to ``OPEN_TAB_SPLIT_FRACTION`` of the dockview
 *       container width, anchored on that chat. The previous broader
 *       fallback (primary's chat, then any open chat) was dropped to
 *       avoid landing one agent's service next to a different agent's
 *       chat just because the requester's chat happened to be closed.
 *    3. Otherwise, add a plain iframe tab with dockview's default placement.
 *  Callers are responsible for any registration / validity check on the
 *  ref before invoking this (e.g. ``handleOpen`` drops unregistered
 *  services), since the WS broadcast itself is fire-and-forget. */
function handleOpenPanelRequest(
  ref: string,
  requesterAgentId: string,
  forceNewGroup: boolean,
  panelIdHint?: string,
): void {
  if (!dockview) return;

  const chatPanelId = findAnchorChatPanelId(requesterAgentId);
  if (chatPanelId === null) {
    addPanelForRef(ref, requesterAgentId, { panelIdHint });
    return;
  }
  // Default: tab into an existing group to the right of the anchor chat
  // if one is open. Callers pass ``forceNewGroup`` to demand a fresh
  // column instead. See ``findSiblingGroupInDirection`` for the
  // adjacency rule.
  const anchorPanel = dockview.panels.find((p) => p.id === chatPanelId);
  const anchorGroupId = anchorPanel?.api.group.id ?? null;
  const sibling =
    !forceNewGroup && anchorGroupId !== null ? findSiblingGroupInDirection(anchorGroupId, "right") : null;
  if (sibling !== null) {
    addPanelForRef(ref, requesterAgentId, { position: { referenceGroup: sibling.id }, panelIdHint });
    return;
  }
  const containerWidth = dockviewContainer?.getBoundingClientRect().width ?? 0;
  const initialWidth = containerWidth > 0 ? Math.round(containerWidth * OPEN_TAB_SPLIT_FRACTION) : undefined;
  addPanelForRef(ref, requesterAgentId, {
    position: { referencePanel: chatPanelId, direction: "right" },
    initialWidth,
    panelIdHint,
  });
}

export function openIframeTabForAgent(agentId: string, url: string, title: string): void {
  if (!dockview) return;
  const existing = dockview.panels.find((p) => {
    const pp = panelParams.get(p.id);
    return pp?.panelType === "iframe" && pp.agentId === agentId && pp.url === url;
  });
  if (existing) {
    if (!existing.api.isActive) {
      dockview.setActivePanel(existing);
    }
    return;
  }
  const panelId = `iframe-agent-${agentId}-${Date.now()}`;
  const params: PanelParams = { panelType: "iframe", agentId, url, title };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "iframe",
    title,
    params,
  });
  appendPanelToEverythingProject(panelId);
}

export function openSubagentTab(agentId: string, subagentSessionId: string, description: string): void {
  if (!dockview) return;

  const existingPanel = dockview.panels.find((p) => {
    const params = panelParams.get(p.id);
    return params?.panelType === "subagent" && params.subagentSessionId === subagentSessionId;
  });
  if (existingPanel) {
    dockview.setActivePanel(existingPanel);
    return;
  }

  const panelId = `subagent-${agentId}-${subagentSessionId}`;
  const params: PanelParams = {
    panelType: "subagent",
    agentId,
    subagentSessionId,
    title: description,
  };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "subagent",
    title: description,
    params,
  });
  appendPanelToEverythingProject(panelId);
}

/** Open a new tab of ``tabType`` in the active project, exactly as the "+"
 *  menu's "New ..." items do.
 *
 *  Exported for the machine rail, which offers the same starting points as
 *  one-click rows. A chat and a browser open their naming modal first -- the
 *  rail is not a place to type a name -- while a terminal allocates its tmux
 *  session straight away. */
export function openTabOfType(tabType: QuickAddTabType): void {
  // Rail rows always land in the active group, so the target group the modals
  // below read (the same field the "+" menu fills in) is cleared rather than set.
  newTabTargetGroup = null;
  if (tabType === "chat") {
    showNewChatModal = true;
    m.redraw();
    return;
  }
  if (tabType === "terminal") {
    void openNewTerminal();
    return;
  }
  if (tabType === "browser") {
    // Clean open, as in the "+" menu: drop any leftover failure pre-fill so the
    // modal fetches a fresh random name and shows no error.
    newBrowserPrefillName = null;
    newBrowserError = null;
    showNewBrowserModal = true;
    m.redraw();
    return;
  }
  // What is left ("files") is not a tab type the workspace builds itself -- it
  // is whichever app of that name the machine runs, so it opens through the
  // same path as the rail's app rows, and opens nothing where none runs.
  const backingApp = getApps().find((app) => app.name === tabType);
  if (backingApp !== undefined) openAppTab(backingApp);
}

/** Open ``app``'s pane in the active project, focusing the one already open
 *  rather than stacking a second. Exported for the machine rail and the
 *  all-apps picker; the "+" menu's app entries create the same panel (it just
 *  hides an app that already has a tab instead of focusing it). */
export function openAppTab(app: AppEntry): void {
  if (!dockview) return;
  const openPanelId = findIframePanelIdForService(app.name);
  const openPanel = openPanelId === null ? undefined : dockview.panels.find((p) => p.id === openPanelId);
  if (openPanel !== undefined) {
    dockview.setActivePanel(openPanel);
    return;
  }
  openIframeTab(deriveServiceOrigin(labelForService(app.name)), app.name, "iframe", app.name);
}

function buildLayoutPayload(): SavedLayout | null {
  if (!dockview) return null;
  const serializedParams: Record<string, PanelParams> = {};
  for (const [id, params] of panelParams) {
    serializedParams[id] = params;
  }
  return { dockview: dockview.toJSON(), panelParams: serializedParams };
}

// ---------- Everything-project bookkeeping ----------

// Serializes the read-modify-write appends into Everything's stored content.
// Every step swallows its own failures, so the chain never settles rejected.
let everythingWriteQueue: Promise<void> = Promise.resolve();

// One node of a serialized dockview grid. Named off the serialized shape
// rather than imported because dockview-core does not re-export the leaf's
// group-state type from its entrypoint.
type SerializedGridNode = SerializedDockview["grid"]["root"];

/** The first leaf group of a serialized grid in depth-first order, or null
 *  when the grid holds no groups at all (an emptied workspace). */
function firstSerializedGroup(node: SerializedGridNode): { views: string[]; activeView?: string } | null {
  if (!Array.isArray(node.data)) return node.data;
  for (const child of node.data) {
    const group = firstSerializedGroup(child);
    if (group !== null) return group;
  }
  return null;
}

/** Add ``panelId`` to ``content``'s stored grid, creating a group for it when
 *  the stored grid has none. Returns false when the panel was already there,
 *  so the caller can skip a pointless write. */
function appendPanelToSavedLayout(content: SavedLayout, panelId: string, panelState: GroupviewPanelState): boolean {
  if (content.dockview.panels[panelId] !== undefined) return false;
  const grid = content.dockview.grid;
  const group = firstSerializedGroup(grid.root);
  if (group !== null) {
    // Appended to the group's tab strip WITHOUT touching ``activeView``: the
    // user is not looking at this project, so the tab they left focused there
    // stays focused.
    group.views.push(panelId);
  } else if (Array.isArray(grid.root.data)) {
    // The stored grid holds no group at all (Everything has been emptied, or
    // was seeded by us). The root splitview runs along its orientation, so a
    // child's size is measured on that axis.
    grid.root.data.push({
      type: "leaf",
      data: { id: `group-${panelId}`, views: [panelId], activeView: panelId },
      size: grid.orientation === Orientation.HORIZONTAL ? grid.width : grid.height,
    });
  } else {
    return false;
  }
  content.dockview.panels[panelId] = panelState;
  return true;
}

/**
 * Also record a freshly-opened tab in the Everything project's stored content.
 *
 * Membership is implicit -- a tab is "in" a project exactly when a panel for it
 * lives in that project's saved state -- so Everything only stays unfiltered if
 * every new tab is written into it as well as into the project it was opened
 * in. Everything keeps its own arrangement, so the panel is appended to its
 * stored grid rather than overwriting it with ours.
 *
 * Best-effort by construction: it runs after the tab is already open and
 * swallows every failure, because failing to reach the server must never stop
 * a tab from opening.
 */
function appendPanelToEverythingProject(panelId: string): void {
  if (!dockview) return;
  // Nothing mounted means no project registry was reachable at startup, so
  // there is no Everything to write to either.
  if (mountedProjectId === null || mountedProjectId === EVERYTHING_PROJECT_ID) return;
  // dockview's own serialization of the panel, so Everything restores it
  // exactly as this project does (component, renderer, title, params).
  const panelState = dockview.toJSON().panels[panelId];
  const params = panelParams.get(panelId);
  if (panelState === undefined || params === undefined) return;
  // Each append is a read-modify-write of one shared document, so they are
  // queued rather than run concurrently: two tabs opened in quick succession
  // would otherwise both read the same pre-append content and the second write
  // would drop the first tab.
  everythingWriteQueue = everythingWriteQueue.then(async () => {
    try {
      const stored = (await fetchProjectContent(EVERYTHING_PROJECT_ID)) as SavedLayout | null;
      const content: SavedLayout = stored ?? {
        dockview: { grid: emptySerializedGrid(), panels: {} },
        panelParams: {},
      };
      if (!appendPanelToSavedLayout(content, panelId, panelState)) return;
      content.panelParams[panelId] = params;
      await autosaveProject(EVERYTHING_PROJECT_ID, content, getClientId());
    } catch {
      // Everything picks the tab up the next time it is saved from a client
      // that has it open; never block (or fail) the tab that was just opened.
    }
  });
}

/** An empty grid to seed Everything with when it has never been saved. Its
 *  dimensions come from the live dockview so the seeded content is laid out
 *  against a plausible viewport rather than a zero-sized one. */
function emptySerializedGrid(): SerializedDockview["grid"] {
  const live = dockview?.toJSON().grid;
  return {
    root: { type: "branch", data: [] },
    width: live?.width ?? 0,
    height: live?.height ?? 0,
    orientation: live?.orientation ?? Orientation.HORIZONTAL,
  };
}

async function saveLayout(): Promise<void> {
  if (!dockview) return;
  const targetProjectId = mountedProjectId;
  if (targetProjectId === null) return;
  if (Date.now() < suppressAutosaveUntilMs) return;
  const payload = buildLayoutPayload();
  if (payload === null) return;
  const serialized = JSON.stringify(payload);
  // Content guard: an unchanged layout is neither re-persisted nor
  // re-broadcast, so remote re-applies cannot echo back and forth.
  if (serialized === lastPersistedLayoutJson) return;

  try {
    await autosaveProject(targetProjectId, payload, getClientId());
    lastPersistedLayoutJson = serialized;
  } catch {
    // The save is best-effort (e.g. the project was deleted mid-flight; the
    // deletion broadcast switches us to the fallback).
  }
}

function scheduleSave(): void {
  if (saveTimer !== null) {
    clearTimeout(saveTimer);
  }
  saveTimer = setTimeout(() => {
    saveTimer = null;
    saveLayout();
  }, AUTOSAVE_DEBOUNCE_MS);
}

/** Flush a pending debounced autosave now. Called before switching layouts
 *  so edits made just before the switch land in the layout they were made
 *  in, never in the one being switched to. */
async function flushPendingSave(): Promise<void> {
  if (saveTimer !== null) {
    clearTimeout(saveTimer);
    saveTimer = null;
    await saveLayout();
  }
}

/** Mark ``content`` as what the server currently holds for the active
 *  project, so the content guard in saveLayout can skip no-op persists. */
function markServerContent(content: SavedLayout | null): void {
  lastPersistedLayoutJson = content === null ? null : JSON.stringify(content);
}

/** Open the autosave-suppression window used when applying content that
 *  arrived over a ``project_saved`` broadcast: the apply (and its follow-on
 *  resize events) must settle without re-persisting, or two clients with
 *  different window sizes would ping-pong saves at each other. User-driven
 *  applies (initial load, load/switch) do NOT suppress -- their follow-on
 *  autosave is what materializes a fresh layout's content file. */
function beginRemoteApplySuppression(): void {
  suppressAutosaveUntilMs = Date.now() + REMOTE_APPLY_SUPPRESS_MS;
}

async function refreshProjectsList(): Promise<void> {
  const listResponse = await fetchProjectsList();
  availableProjects = listResponse.projects;
  m.redraw();
}

function displayNameForProject(projectId: string): string {
  return availableProjects.find((project) => project.project_id === projectId)?.name ?? projectId;
}

/** The active project's registry entry, or null before the registry has loaded
 *  (or when the client is on a project that has since been deleted). Exported
 *  for the chrome around the workspace -- the sidebar renders the active
 *  project's squiggle and name from it. */
export function getActiveProject(): ProjectInfo | null {
  const activeProjectId = getActiveProjectId();
  return availableProjects.find((project) => project.project_id === activeProjectId) ?? null;
}

/**
 * Mount ``saved`` into the dockview, replacing whatever is currently shown.
 * ``null`` (a layout with no saved content, or none could be fetched)
 * renders the fresh-workspace state: the initial welcome chat auto-opens
 * (or the empty-state overlay waits for it). Mirrors the restore semantics
 * that previously lived inline in ``initializeDockview``.
 */
function applyLayoutContent(saved: SavedLayout | null): void {
  if (!dockview) return;
  const dv = dockview;
  awaitingInitialChat = false;

  // Tear the outgoing layout down BEFORE seeding the incoming params.
  // ``fromJSON`` disposes the current panels before creating the new ones, and
  // ``onDidRemovePanel`` deletes each disposed panel's ``panelParams`` entry.
  // Panel ids are deterministic (``chat-<agent-id>``,
  // ``terminal-session-<name>``), so a panel present in BOTH layouts would have
  // its freshly-seeded entry deleted mid-restore and come back with no params.
  // Clearing first means every disposal fires against the outgoing state we are
  // discarding anyway, and nothing can race the fresh map.
  dv.clear();
  panelParams.clear();

  let savedHadAnyPanels = false;
  if (saved) {
    for (const [id, params] of Object.entries(saved.panelParams)) {
      panelParams.set(id, params);
    }
    // Rebuild each restored terminal's ttyd url with a fresh per-tab id, so
    // the ttyd ``session`` dispatch reattaches to the live tmux session -- or
    // recreates it as a fresh shell if the tmux server was torn down since the
    // layout was saved (e.g. a container restart). The fresh id keeps the
    // pty->tab mapping (for live title tracking) accurate for this connection.
    // Done before ``fromJSON`` so the terminal renderer mounts on the new url.
    //
    // Service and agent-terminal urls are re-derived in the same pass: a
    // persisted url is an absolute origin from whichever host saved the
    // layout, so it is only a stale hint -- the panel's identity
    // (``serviceName``, or the ttyd agent-dispatch args) is authoritative and
    // the url is rebuilt from it on the current host, preserving any
    // ``?query`` (e.g. a browser pane's ``session=<name>``). This is what
    // keeps saved layouts portable across hosts and shares.
    for (const [, params] of panelParams) {
      if (params.terminalSessionName) {
        params.terminalId = mintTerminalId();
        params.url = buildSessionTerminalUrl(params.terminalSessionName, params.terminalId, primaryWorkDir());
      } else if (params.serviceName) {
        params.url = `${deriveServiceOrigin(labelForService(params.serviceName))}${urlQuerySuffix(params.url)}`;
      } else if (params.url) {
        const rebuiltTerminalUrl = rebuildAgentTerminalUrl(params.url);
        if (rebuiltTerminalUrl !== null) params.url = rebuiltTerminalUrl;
      }
    }
    try {
      dv.fromJSON(saved.dockview);
    } catch {
      panelParams.clear();
      dv.clear();
    }
    savedHadAnyPanels = dv.panels.length > 0;
    // Strip any chat panels that point at the is_primary services agent.
    // Older saved layouts (or layouts saved by the previous code path
    // that auto-opened the primary agent's chat) may carry a chat-
    // <services-agent-id> panel; we don't want to surface that ever.
    //
    // This MUST be limited to chat panels. Iframe tabs (terminals,
    // apps, custom URLs) opened via openIframeTab() set
    // `agentId` to the primary agent id as a placeholder owner, so a
    // bare `agentId === primaryId` check would wrongly strip every
    // terminal/app/URL tab on each restore.
    const primaryId = getPrimaryAgentId();
    if (primaryId) {
      for (const panel of dv.panels.slice()) {
        const params = panelParams.get(panel.id);
        if (params?.panelType !== "chat") continue;
        const targetId = params.chatAgentId ?? params.agentId;
        if (targetId === primaryId) {
          dv.removePanel(panel);
        }
      }
    }
  }

  // Auto-open the initial chat tab when:
  //   - the layout has no saved content yet (fresh layout), OR
  //   - the saved content existed but all its panels were services-agent
  //     panels we just stripped above.
  // If the saved content was a non-empty layout that the user
  // intentionally emptied (savedHadAnyPanels=false because saved
  // existed but had zero panels), we respect that and leave the
  // empty state visible.
  const shouldAutoOpen = saved === null || (savedHadAnyPanels && dv.panels.length === 0);
  if (shouldAutoOpen) {
    if (!openInitialChatTab()) {
      awaitingInitialChat = true;
    }
  }
  updateEmptyState();
}

/** Record ``projectId`` as this browser's active project.
 *
 *  The active project IS this client's dockview state, so the id is mirrored
 *  onto the client-identity layout slug as well: that is the value
 *  ``reportClientState`` registers with the server and the one a sent chat
 *  message is attributed to, and leaving it unset would deregister the client
 *  entirely. */
function setActiveProject(projectId: string): void {
  setActiveProjectId(projectId);
  setActiveLayoutSlug(projectId);
  mountedProjectId = projectId;
}

/**
 * Pick this client's initial project (stored per-browser choice, else
 * Everything, else the first project), register it with the server, and mount
 * its content. Runs once at startup, after the dockview exists.
 */
async function initializeActiveProject(): Promise<void> {
  const listResponse = await fetchProjectsList();
  availableProjects = listResponse.projects;
  const chosen = chooseInitialProject(availableProjects, getStoredProjectId());
  if (chosen === null) {
    // No projects at all (server unreachable / no primary agent): run with
    // the fresh-workspace state; nothing persists.
    applyLayoutContent(null);
    m.redraw();
    return;
  }
  setActiveProject(chosen.project_id);
  reportClientState();
  const saved = (await fetchProjectContent(chosen.project_id)) as SavedLayout | null;
  markServerContent(saved);
  applyLayoutContent(saved);
  m.redraw();
}

/**
 * Switch this client onto another project: flush pending edits into the old
 * project, repoint the autosave target, tell the server (which records the
 * switch event), and mount the new project's content.
 *
 * Exported for the project picker, which owns the *choice* and hands the
 * actual load over here.
 */
export async function switchToProject(projectId: string): Promise<void> {
  if (!dockview) return;
  const previousProjectId = mountedProjectId ?? getActiveProjectId();
  if (previousProjectId === projectId) return;
  await flushPendingSave();
  setActiveProject(projectId);
  reportClientState(previousProjectId);
  const saved = (await fetchProjectContent(projectId)) as SavedLayout | null;
  markServerContent(saved);
  applyLayoutContent(saved);
  m.redraw();
}

/** React to project registry / sync broadcasts from other clients + agents.
 *  The WebSocket dispatcher calls this with each ``project_*`` event. */
export function handleProjectSyncEvent(event: ProjectSyncEvent): void {
  if (event.kind === "saved") {
    void refreshProjectsList();
    // Live sync: another client saved the project we're on -- re-apply it.
    // Skipping our own saves (by client id) is the originator half of the
    // echo suppression; the content guard in saveLayout is the other half.
    if (event.projectId === mountedProjectId && event.savedByClientId !== getClientId()) {
      void (async () => {
        const saved = (await fetchProjectContent(event.projectId)) as SavedLayout | null;
        markServerContent(saved);
        beginRemoteApplySuppression();
        applyLayoutContent(saved);
        m.redraw();
      })();
    }
    return;
  }
  if (event.kind === "deleted") {
    // Read the deleted project's name before the (async) refresh drops it
    // from the cache.
    const deletedName = displayNameForProject(event.projectId);
    void refreshProjectsList();
    if (event.projectId === mountedProjectId) {
      void switchToProject(event.fallbackId).then(() => {
        alert(`Project "${deletedName}" was deleted; switched to "${displayNameForProject(event.fallbackId)}".`);
      });
    }
    return;
  }
  // Renamed / restyled: the content is untouched, so re-list and let the
  // picker and sidebar repaint from the fresh registry.
  void refreshProjectsList();
}

// ---------- Agent-driven layout op handlers ----------

/** First eight hex chars of the panel id's SHA-256, matching the
 *  server-side ``_short_hash`` used to build ``terminal:`` / ``url:`` refs. */
async function shortHash(panelId: string): Promise<string> {
  const data = new TextEncoder().encode(panelId);
  const buffer = await crypto.subtle.digest("SHA-256", data);
  const bytes = new Uint8Array(buffer);
  let hex = "";
  for (let i = 0; i < 4; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}

/** Map a ``direction`` from the layout op (left/right/above/below) onto
 *  dockview's ``Position`` enum used by ``panel.api.moveTo``. */
function directionToPosition(direction: string): "top" | "bottom" | "left" | "right" {
  switch (direction) {
    case "above":
      return "top";
    case "below":
      return "bottom";
    case "left":
      return "left";
    case "right":
      return "right";
    default:
      return "right";
  }
}

/** Resolve a layout-op ref (or the literal "self") to a live dockview
 *  panel id. Returns null when no matching panel is currently open --
 *  callers decide whether that's fatal (close/focus) or a no-op cue to
 *  fall back to a creation path (open/split). */
async function resolveRefToPanelId(ref: string, requesterAgentId: string): Promise<string | null> {
  if (!dockview) return null;
  if (ref === "self") {
    // ``self`` is the *identity* ref for the caller's own chat panel
    // (``chat-<requesterAgentId>``). Returns null when the requester
    // didn't set ``MNGR_AGENT_ID`` or when their chat tab isn't open.
    // All layout ops (including ``relative_to=self`` on split/move)
    // honor this strict identity to avoid silently retargeting another
    // agent's chat.
    if (!requesterAgentId) return null;
    const candidate = chatPanelId(requesterAgentId);
    return dockview.panels.find((p) => p.id === candidate) ? candidate : null;
  }
  if (ref.startsWith("service:")) {
    // Handles both the bare ``service:web`` (dedup by serviceName) and the
    // session-specific ``service:browser?session=2`` (dedup by URL) forms.
    return findIframePanelIdForServiceRef(ref.substring("service:".length));
  }
  if (ref.startsWith("https://")) {
    // An external-URL ref resolves to whichever ad-hoc iframe tab is
    // currently pointed at that exact URL (focus-if-open dedup). Once
    // open, the panel is also addressable by its ``url:<hash>`` ref.
    return findIframePanelIdForUrl(ref);
  }
  if (ref.startsWith("chat:")) {
    const agentName = ref.substring("chat:".length);
    const agent = getAgents().find((a) => a.name === agentName);
    if (!agent) return null;
    const candidate = chatPanelId(agent.id);
    return dockview.panels.find((p) => p.id === candidate) ? candidate : null;
  }
  if (ref.startsWith("chat-terminal:")) {
    // Resolve by URL: ``chat-terminal:<name>`` addresses the singleton
    // iframe pointed at ``buildAgentTerminalUrl(name)``. ``findIframe
    // PanelIdForUrl`` returns null when no such panel is currently open.
    const agentName = ref.substring("chat-terminal:".length);
    return findIframePanelIdForUrl(buildAgentTerminalUrl(agentName));
  }
  if (ref.startsWith("subagent:")) {
    const sessionId = ref.substring("subagent:".length);
    for (const [panelId, p] of panelParams) {
      if (p.panelType === "subagent" && p.subagentSessionId === sessionId) return panelId;
    }
    return null;
  }
  if (ref.startsWith("url:") || ref.startsWith("terminal:")) {
    const hash = ref.split(":")[1] ?? "";
    for (const panelId of panelParams.keys()) {
      const candidateHash = await shortHash(panelId);
      if (candidateHash === hash) return panelId;
    }
    return null;
  }
  return null;
}

/** Resolve a ``service:<name>[/<path>]`` shorthand URL (sent by
 *  ``replace-url``) to a URL on the service's derived origin. Plain
 *  ``https://`` URLs pass through. */
function resolveReplaceUrl(url: string): string {
  if (url.startsWith("service:")) {
    const remainder = url.substring("service:".length);
    const slashIndex = remainder.indexOf("/");
    if (slashIndex === -1) return deriveServiceOrigin(labelForService(remainder));
    const serviceName = remainder.substring(0, slashIndex);
    const path = remainder.substring(slashIndex + 1);
    return `${deriveServiceOrigin(labelForService(serviceName))}${path}`;
  }
  return url;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

async function handleLayoutOp(event: LayoutOpEvent): Promise<void> {
  if (!dockview) return;
  const requesterAgentId = event.requesterAgentId;
  switch (event.op) {
    case "open":
      await handleOpen(event.args, requesterAgentId);
      return;
    case "focus":
      await handleFocus(event.args, requesterAgentId);
      return;
    case "split":
      await handleSplit(event.args, requesterAgentId);
      return;
    case "close":
      await handleClose(event.args, requesterAgentId);
      return;
    case "move":
      await handleMove(event.args, requesterAgentId);
      return;
    case "rename":
      await handleRename(event.args, requesterAgentId);
      return;
    case "maximize":
      await handleMaximize(event.args, requesterAgentId);
      return;
    case "restore":
      handleRestore();
      return;
    case "replace-url":
      await handleReplaceUrl(event.args, requesterAgentId);
      return;
    case "refresh":
      await handleRefresh(event.args, requesterAgentId);
      return;
    case "reload_system_interface":
      reloadInterface();
      return;
  }
}

async function handleOpen(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  const ref = asString(args.ref);
  if (!ref || !dockview) return;
  // ``service:terminal`` is the one creation path that bypasses dedup:
  // each ``open terminal`` adds a fresh tab, mirroring the UI's "New
  // terminal" button. The broadcast endpoint allocates ``args.panel_id``
  // so it can return the resulting ``terminal:<hash>`` ref synchronously.
  if (ref === "service:terminal") {
    const panelIdHint = asString(args.panel_id) ?? undefined;
    handleOpenPanelRequest(ref, requesterAgentId, args.new_group === true, panelIdHint);
    return;
  }
  const existing = await resolveRefToPanelId(ref, requesterAgentId);
  if (existing !== null) {
    const panel = dockview.panels.find((p) => p.id === existing);
    if (panel) dockview.setActivePanel(panel);
    return;
  }
  if (ref.startsWith("service:")) {
    // Drop silently if the service isn't registered in ``apps``
    // yet -- the script polls registration, but the broadcast races it.
    // Strip any ``?session=`` suffix (browser fleet) before the lookup:
    // registration is per-service, not per-session.
    const serviceName = parseServiceRefBody(ref.substring("service:".length)).name;
    if (!getApps().find((a) => a.name === serviceName)) return;
    handleOpenPanelRequest(ref, requesterAgentId, args.new_group === true);
    return;
  }
  if (ref.startsWith("https://")) {
    handleOpenPanelRequest(ref, requesterAgentId, args.new_group === true);
    return;
  }
  if (ref.startsWith("chat:")) {
    // Drop silently if no agent with this name is currently known --
    // ``addPanelForRef``'s chat branch is responsible for the actual
    // dockview.addPanel call so all three creation paths (service /
    // https / chat) share the same anchor-positioning and
    // share-existing-group defaults.
    const agentName = ref.substring("chat:".length);
    if (!getAgents().find((a) => a.name === agentName)) return;
    handleOpenPanelRequest(ref, requesterAgentId, args.new_group === true);
    return;
  }
  if (ref.startsWith("chat-terminal:")) {
    // Same drop-on-unknown-agent rule as ``chat:`` -- the underlying
    // panel is the per-agent terminal iframe, addressable by name.
    const agentName = ref.substring("chat-terminal:".length);
    if (!getAgents().find((a) => a.name === agentName)) return;
    handleOpenPanelRequest(ref, requesterAgentId, args.new_group === true);
    return;
  }
  // Other ref kinds (subagent/terminal/url:<hash>) are not creatable from
  // an ``open`` op: their stable refs only exist after creation through
  // the surrounding code paths (e.g. SubagentView, "New URL" dialog).
}

async function handleFocus(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  if (!ref) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) dockview.setActivePanel(panel);
}

async function handleSplit(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  const relativeTo = asString(args.relative_to);
  const direction = asString(args.direction) ?? "right";
  const ratio = asNumber(args.ratio);
  const forceNewGroup = args.new_group === true;
  if (!ref || !relativeTo) return;

  // ``relative_to=self`` strictly anchors against the requester's own chat
  // panel. If their chat isn't open (or they didn't set ``MNGR_AGENT_ID``),
  // the op is a no-op rather than landing next to some other agent's chat.
  const referencePanelId = await resolveRefToPanelId(relativeTo, requesterAgentId);
  if (referencePanelId === null) return;

  if (
    !ref.startsWith("service:") &&
    !ref.startsWith("chat:") &&
    !ref.startsWith("chat-terminal:") &&
    !ref.startsWith("https://")
  ) {
    // ``split`` creates new service, chat, chat-terminal, and ad-hoc
    // external-URL (``https://``) panels. Subagent panels and existing
    // URL/terminal panels addressed by ``url:<hash>`` / ``terminal:<hash>``
    // are created through other UI paths and only addressable once they
    // exist. Fresh anonymous terminals come in as ``service:terminal``.
    return;
  }

  const containerRect = dockviewContainer?.getBoundingClientRect();
  const sizes = computeInitialSize(direction, ratio, containerRect);

  const referencePanel = dockview.panels.find((p) => p.id === referencePanelId);
  const anchorGroupId = referencePanel?.api.group.id ?? null;

  // ``service:terminal`` is the one creation path the server pre-allocates
  // a panel id for (so its HTTP response can return the resulting
  // ``terminal:<hash>`` ref); thread the hint through ``addPanelForRef``.
  const panelIdHint = ref === "service:terminal" ? (asString(args.panel_id) ?? undefined) : undefined;

  // ``direction: "within"`` tabs the new panel into the anchor's own
  // group (no sibling lookup, no size hints, ``new_group`` ignored).
  // This is the unambiguous "put X in the same group as Y" surface --
  // the cardinal directions all describe *adjacent* groups.
  if (isWithinDirection(direction)) {
    if (anchorGroupId === null) return;
    addPanelForRef(ref, requesterAgentId, { position: { referenceGroup: anchorGroupId }, panelIdHint });
    return;
  }

  // Default: when a group already lives in the requested direction
  // relative to the anchor, tab the new panel into that group instead
  // of carving a new column. ``new_group`` opts back in to the
  // always-fresh-column behavior.
  const directionArg = directionFromArg(direction);
  const sibling =
    !forceNewGroup && anchorGroupId !== null ? findSiblingGroupInDirection(anchorGroupId, directionArg) : null;
  const positionOptions =
    sibling !== null ? { referenceGroup: sibling.id } : { referencePanel: referencePanelId, direction: directionArg };
  // Size hints only apply when we're carving a new group; tabbing into
  // an existing group ignores them anyway, so omit to keep intent clear.
  const sizeOptions = sibling !== null ? {} : sizes;

  // service:, chat:, and https:// all route through ``addPanelForRef``
  // which handles dedup (focus existing instead of duplicating) +
  // panelParams bookkeeping + the actual addPanel invocation.
  addPanelForRef(ref, requesterAgentId, { position: positionOptions, ...sizeOptions, panelIdHint });
}

function directionFromArg(direction: string): "left" | "right" | "above" | "below" {
  if (direction === "left" || direction === "right" || direction === "above" || direction === "below") {
    return direction;
  }
  return "right";
}

/** True for the synthetic ``within`` direction, which means "tab into the
 *  anchor's own group" rather than naming an adjacent group. Routes
 *  ``split`` / ``move`` through dockview's ``referenceGroup`` /
 *  ``moveTo({ group })`` branch with the anchor's group id, bypassing
 *  ``findSiblingGroupInDirection``. ``new_group`` is meaningless here. */
function isWithinDirection(direction: string): boolean {
  return direction === "within";
}

function computeInitialSize(
  direction: string,
  ratio: number | null,
  containerRect: DOMRect | undefined,
): { initialWidth?: number; initialHeight?: number } {
  if (ratio === null || !containerRect) return {};
  if (direction === "above" || direction === "below") {
    const h = containerRect.height > 0 ? Math.round(containerRect.height * ratio) : undefined;
    return h ? { initialHeight: h } : {};
  }
  const w = containerRect.width > 0 ? Math.round(containerRect.width * ratio) : undefined;
  return w ? { initialWidth: w } : {};
}

async function handleClose(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  if (!ref) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) dockview.removePanel(panel);
}

async function handleMove(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  const relativeTo = asString(args.relative_to);
  const direction = asString(args.direction);
  const forceNewGroup = args.new_group === true;
  if (!ref || !relativeTo || !direction) return;
  const targetPanelId = await resolveRefToPanelId(ref, requesterAgentId);
  // ``relative_to`` follows the same strict-identity rule as ``handleSplit``:
  // ``self`` resolves to the requester's chat or nothing.
  const referencePanelId = await resolveRefToPanelId(relativeTo, requesterAgentId);
  if (targetPanelId === null || referencePanelId === null) return;
  const targetPanel = dockview.panels.find((p) => p.id === targetPanelId);
  const referencePanel = dockview.panels.find((p) => p.id === referencePanelId);
  if (!targetPanel || !referencePanel) return;
  const anchorGroupId = referencePanel.api.group.id;

  // ``direction: "within"`` moves the panel into the anchor's own group
  // as another tab. ``new_group`` is meaningless here -- we always tab
  // into the existing anchor group.
  if (isWithinDirection(direction)) {
    // Same self-move guard as the cardinal-direction path below: if the
    // target is already in the anchor's group as a sole occupant, the
    // dockview ``moveTo`` would empty + dispose the source before adding
    // to the destination (same group), dropping the panel from the layout.
    if (targetPanel.api.group.id === referencePanel.api.group.id) return;
    targetPanel.api.moveTo({ group: referencePanel.api.group });
    return;
  }

  // Same share-group default as handleSplit: if a group already lives
  // in the requested direction, drop the panel into it as a tab unless
  // the caller asked for a brand-new group.
  const directionArg = directionFromArg(direction);
  const sibling = !forceNewGroup ? findSiblingGroupInDirection(anchorGroupId, directionArg) : null;
  if (sibling !== null) {
    const siblingGroup = dockview.groups.find((g) => g.id === sibling.id);
    if (siblingGroup) {
      // Guard against tabbing a sole-occupant panel into its own group:
      // dockview's ``moveTo`` removes from the source group first, which
      // empties + disposes the source. If source === destination, the
      // destination is now disposed and the panel is dropped from the
      // layout entirely. Treat the request as a no-op instead.
      if (siblingGroup.id === targetPanel.api.group.id) return;
      targetPanel.api.moveTo({ group: siblingGroup });
      return;
    }
  }
  targetPanel.api.moveTo({
    group: referencePanel.api.group,
    position: directionToPosition(direction),
  });
}

async function handleRename(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  const title = asString(args.title);
  if (!ref || !title) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (!panel) return;
  panel.api.setTitle(title);
  const params = panelParams.get(panelId);
  if (params) {
    params.title = title;
  }
}

async function handleMaximize(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  if (!ref) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) panel.api.maximize();
}

function handleRestore(): void {
  if (!dockview) return;
  for (const panel of dockview.panels) {
    if (panel.api.isMaximized()) {
      panel.api.exitMaximized();
      return;
    }
  }
}

async function handleReplaceUrl(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  const ref = asString(args.ref);
  const url = asString(args.url);
  if (!ref || !url) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const params = panelParams.get(panelId);
  if (!params || params.panelType !== "iframe") return;
  params.url = resolveReplaceUrl(url);
  m.redraw();
  scheduleSave();
}

async function handleRefresh(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  const ref = asString(args.ref);
  if (!ref) return;
  if (ref.startsWith("service:")) {
    reloadIframesForService(ref.substring("service:".length));
    return;
  }
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const params = panelParams.get(panelId);
  if (!params || params.panelType !== "iframe") return;
  reloadIframeForPanel(panelId);
}

/** Build a dockview content renderer for an iframe panel that re-reads
 *  ``panelParams[panelId]`` on every mithril redraw. This keeps the
 *  iframe in sync with agent-driven mutations to ``url``/``title`` so
 *  ``replace-url`` doesn't need to remove-and-recreate the panel. */
function createReactiveIframeRenderer(panelId: string): IContentRenderer {
  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  element.style.display = "flex";
  element.style.flexDirection = "column";
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const iframePanelComponent: m.ComponentTypes<any, any> = IframePanel;
  return {
    element,
    init() {
      m.mount(element, {
        view: () => {
          const p = panelParams.get(panelId);
          return m(iframePanelComponent, {
            url: p?.url ?? "",
            title: p?.title ?? "Tab",
            serviceName: p?.serviceName,
            panelId,
          });
        },
      });
    },
    dispose() {
      m.mount(element, null);
    },
  };
}

/** Like ``createReactiveIframeRenderer`` but stacks the terminal lifecycle
 *  banner above the iframe. Reads ``panelParams[panelId]`` live so the
 *  async-allocated (agent-driven) and layout-restore url rewrites re-render
 *  the iframe with the new src. */
function createReactiveTerminalRenderer(panelId: string): IContentRenderer {
  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  element.style.display = "flex";
  element.style.flexDirection = "column";
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const iframePanelComponent: m.ComponentTypes<any, any> = IframePanel;
  return {
    element,
    init() {
      m.mount(element, {
        view: () => {
          const p = panelParams.get(panelId);
          return [
            m(TerminalBanner),
            m(
              "div",
              { style: "flex: 1 1 auto; min-height: 0;" },
              m(iframePanelComponent, {
                url: p?.url ?? "",
                title: p?.title ?? "terminal",
                panelId,
              }),
            ),
          ];
        },
      });
    },
    dispose() {
      m.mount(element, null);
    },
  };
}

// The empty-state overlay sits inside the dockview container as a sibling
// to dockview's own DOM. Shown when panels.length === 0; hidden otherwise.
// Its "+" button opens the SAME dropdown buildDropdownItems() backs in the
// header's add-tab button, so the user has the same set of actions even
// when no tabs are visible.
let emptyStateOverlay: HTMLElement | null = null;
let emptyStateStatus: HTMLElement | null = null;
let emptyStateAction: HTMLButtonElement | null = null;
let emptyStateDropdown: HTMLElement | null = null;

function createEmptyStateOverlay(): HTMLElement {
  const overlay = document.createElement("div");
  overlay.className = "dockview-empty-state";
  overlay.style.position = "absolute";
  overlay.style.inset = "0";
  overlay.style.display = "none";
  overlay.style.alignItems = "center";
  overlay.style.justifyContent = "center";
  overlay.style.pointerEvents = "auto";
  overlay.style.zIndex = "1";

  const card = document.createElement("div");
  card.className = "dockview-empty-state-card";
  card.style.display = "flex";
  card.style.flexDirection = "column";
  card.style.alignItems = "center";
  card.style.padding = "32px 40px";
  card.style.background = "#fff";
  card.style.border = "1px solid #e5e7eb";
  card.style.borderRadius = "12px";
  card.style.boxShadow = "0 4px 12px rgba(0,0,0,0.06)";
  card.style.position = "relative";

  const status = document.createElement("div");
  status.className = "dockview-empty-state-status";
  status.style.marginBottom = "16px";
  status.style.color = "#374151";
  status.style.fontSize = "14px";
  status.textContent = "No tabs open";
  card.appendChild(status);

  const action = document.createElement("button");
  action.className = "dockview-empty-state-action";
  action.style.padding = "10px 20px";
  action.style.fontSize = "16px";
  action.style.fontWeight = "500";
  action.style.background = "#3b82f6";
  action.style.color = "#fff";
  action.style.border = "none";
  action.style.borderRadius = "8px";
  action.style.cursor = "pointer";
  action.textContent = "+ Open new tab";
  card.appendChild(action);

  const dropdown = document.createElement("div");
  dropdown.className = "dockview-empty-state-dropdown";
  dropdown.style.position = "absolute";
  dropdown.style.top = "calc(100% + 8px)";
  dropdown.style.left = "50%";
  dropdown.style.transform = "translateX(-50%)";
  dropdown.style.minWidth = "240px";
  dropdown.style.background = "#fff";
  dropdown.style.border = "1px solid #e5e7eb";
  dropdown.style.borderRadius = "8px";
  dropdown.style.boxShadow = "0 4px 12px rgba(0,0,0,0.08)";
  dropdown.style.padding = "4px 0";
  dropdown.style.display = "none";
  dropdown.style.zIndex = "2";
  card.appendChild(dropdown);

  action.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = dropdown.style.display !== "none";
    if (isOpen) {
      dropdown.style.display = "none";
      return;
    }
    // Same build-now-then-refresh pattern as the header "+" dropdown: render
    // from the cached fleet immediately, then re-render once the live fetch
    // resolves (if still open) so active browsers show up here too.
    renderDropdownItems(dropdown);
    refreshBrowserFleet(() => {
      if (dropdown.style.display !== "none") renderDropdownItems(dropdown);
    });
    refreshTerminalFleet(() => {
      if (dropdown.style.display !== "none") renderDropdownItems(dropdown);
    });
    dropdown.style.display = "block";
  });

  // Close the dropdown when clicking elsewhere.
  document.addEventListener("click", () => {
    dropdown.style.display = "none";
  });

  overlay.appendChild(card);
  emptyStateStatus = status;
  emptyStateAction = action;
  emptyStateDropdown = dropdown;
  return overlay;
}

function updateEmptyState(): void {
  if (!emptyStateOverlay || !dockview) return;
  const isEmpty = dockview.panels.length === 0;
  emptyStateOverlay.style.display = isEmpty ? "flex" : "none";
  if (!isEmpty) {
    if (emptyStateDropdown) emptyStateDropdown.style.display = "none";
    return;
  }
  if (awaitingInitialChat) {
    if (emptyStateStatus) emptyStateStatus.textContent = "Waiting for initial chat agent...";
    if (emptyStateAction) emptyStateAction.style.display = "none";
  } else {
    if (emptyStateStatus) emptyStateStatus.textContent = "No tabs open";
    if (emptyStateAction) emptyStateAction.style.display = "";
  }
}

function closeActiveTabFromEmbedder(): void {
  const activePanel = dockview?.activePanel;
  if (activePanel) activePanel.api.close();
}

function initializeDockview(parentElement: HTMLElement): void {
  if (initialized) return;
  initialized = true;

  dockviewContainer = document.createElement("div");
  dockviewContainer.className = "dockview-agent-container dockview-theme-light";
  dockviewContainer.style.width = "100%";
  dockviewContainer.style.height = "100%";
  dockviewContainer.style.position = "relative";
  parentElement.appendChild(dockviewContainer);

  emptyStateOverlay = createEmptyStateOverlay();
  dockviewContainer.appendChild(emptyStateOverlay);

  // dockview-core's Scrollbar only reads event.deltaY, so mice with a dedicated
  // horizontal scroll wheel (e.g. Logitech MX Master) emit deltaX events that
  // the tab bar never reacts to. Delegate wheel here and translate deltaX into
  // scrollLeft on the tabs container; dockview's own 'scroll' listener on that
  // element will sync its internal offset, keeping the custom scrollbar thumb
  // in step.
  dockviewContainer.addEventListener(
    "wheel",
    (event: WheelEvent) => {
      if (event.deltaX === 0) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const tabsContainer = target.closest<HTMLElement>(".dv-tabs-container");
      if (!tabsContainer || !dockviewContainer?.contains(tabsContainer)) return;
      event.preventDefault();
      tabsContainer.scrollLeft += event.deltaX;
    },
    { passive: false },
  );

  const dv = new DockviewComponent(dockviewContainer, {
    theme: themeLight,
    defaultRenderer: "always",
    defaultTabComponent: "custom",
    createComponent(options) {
      // dockview supplies ``params`` for panels created through ``addPanel``,
      // but NOT for panels it recreates from ``fromJSON`` -- those fall back to
      // the stored entry, and to a re-derivation from the panel id when even
      // that is missing. A panel whose identity cannot be recovered renders an
      // explicit placeholder; it must never silently default to another agent.
      const suppliedParams = (options as unknown as { params?: PanelParams }).params;
      const params = resolvePanelParams(options.id, suppliedParams);
      if (params === null) {
        return createUnrecoverablePanelRenderer(options.id);
      }

      switch (options.name) {
        case "chat":
          return createMithrilRenderer(ChatPanel, {
            agentId: params.chatAgentId ?? params.agentId,
          });

        case "iframe": {
          // Agent-terminal tabs route to AgentTerminalPanel, which starts the
          // agent before attaching its terminal session. They are identified
          // by their URL shape: the terminal service URL plus the ttyd
          // agent-dispatch key (`arg=agent`), which `buildAgentTerminalUrl`
          // constructs and no other iframe URL uses. Terminals are never the
          // target of an agent-driven ``replace-url``, so they don't need the
          // reactive renderer below.
          const iframeUrl = params.url ?? "";
          // Persistent-terminal tabs render the lifecycle banner above a
          // reactive iframe (the url is filled in / rewritten after mount for
          // the agent-driven and layout-restore paths). Identified by the
          // terminal-panel params, which no other iframe sets.
          const isSessionTerminal = isTerminalPanelParams(params);
          if (isSessionTerminal) {
            return createReactiveTerminalRenderer(options.id);
          }
          const isAgentTerminal = iframeUrl.startsWith(getTerminalUrl()) && iframeUrl.includes("arg=agent");
          if (isAgentTerminal) {
            return createMithrilRenderer(AgentTerminalPanel, {
              agentId: params.agentId,
              url: iframeUrl,
              title: params.title ?? "Tab",
            });
          }
          // Pull live values out of ``panelParams`` on every redraw so an
          // agent-driven ``replace-url`` (which mutates the stored
          // params) re-renders the iframe with the new src instead of
          // staying frozen on the initial url captured at mount time.
          return createReactiveIframeRenderer(options.id);
        }

        case "subagent":
          return createMithrilRenderer(SubagentView, {
            agentId: params.agentId,
            subagentSessionId: params.subagentSessionId ?? "",
          });

        default:
          // An unknown component name: the layout references a panel kind this
          // build does not have. Say so rather than rendering a chat for the
          // wrong agent.
          return createUnrecoverablePanelRenderer(options.id);
      }
    },
    createTabComponent(options) {
      return createCustomTab(options);
    },
    createLeftHeaderActionComponent(group) {
      return createAddTabButton(group);
    },
  });

  dockview = dv;

  // The embedding minds chrome forwards its close-tab shortcut (Cmd/Ctrl+W
  // while this workspace is displayed) through the embed contract; close the
  // active dockview tab in response.
  setEmbedderMessageHandler(CLOSE_ACTIVE_TAB, closeActiveTabFromEmbedder);

  // Listen for layout changes and auto-save
  dv.api.onDidLayoutChange(() => {
    scheduleSave();
    // Opening, closing, or moving a tab changes the open/visible chat set;
    // report it so the OOM prioritizer re-scores the affected chats.
    reportChatTabActivity();
  });

  // Clean up params on panel removal. We DON'T reopen anything when
  // panels.length hits zero -- instead the empty-state overlay (see
  // below) appears, giving the user the same "+" dropdown actions the
  // dockview header normally hosts. This is the user-visible escape from
  // the previous "system-services keeps coming back" behavior.
  dv.api.onDidRemovePanel((panel) => {
    panelParams.delete(panel.id);
    updateEmptyState();
  });
  dv.api.onDidAddPanel(() => {
    updateEmptyState();
  });

  // While awaitingInitialChat is true, every agents_updated event is
  // another chance for the bootstrap-created chat agent to show up.
  agentsUpdatedListener = () => {
    if (awaitingInitialChat && openInitialChatTab()) {
      awaitingInitialChat = false;
      updateEmptyState();
    }
  };
  addAgentsUpdatedListener(agentsUpdatedListener);

  // Agent-driven layout ops arrive as {type: "layout_op", op, args} on
  // the system-interface WebSocket. ``system/scripts/layout.py`` is the source
  // of those messages; per-op handlers below dispatch on ``event.op``.
  _layoutOpListener = (event: LayoutOpEvent) => {
    void handleLayoutOp(event);
  };
  addLayoutOpListener(_layoutOpListener);

  // Project registry / sync broadcasts: another client saving the project we
  // are mounted on re-applies it here, and a deleted project moves everyone
  // still on it onto the fallback.
  _projectSyncListener = (event: ProjectSyncEvent) => {
    handleProjectSyncEvent(event);
  };
  addProjectSyncListener(_projectSyncListener);

  // Terminal session updates (client switched session / session renamed) push
  // over the same WebSocket; reflect them onto the owning tab's title.
  _terminalSessionListener = (terminalId, sessionId, sessionName) => {
    handleTerminalSessionUpdate(terminalId, sessionId, sessionName);
  };
  addTerminalSessionListener(_terminalSessionListener);

  // Pick this browser's active project and mount its content.
  void initializeActiveProject();
}

/**
 * Take a destroyed tab out of every project's stored content, and out of the
 * dockview if it is still on screen.
 *
 * Closing a tab removes it from the project you are looking at; destroying
 * tears down the thing behind it, so the panel has to leave the projects that
 * are not currently mounted too -- otherwise switching to one of them would
 * restore a tab whose agent, session, or browser no longer exists. The
 * server-side sweep is best-effort: the destroy has already happened, and the
 * projects that still hold the panel drop it the next time they are saved.
 */
async function forgetDestroyedPanel(panelId: string): Promise<void> {
  if (dockview) {
    const panel = dockview.panels.find((p) => p.id === panelId);
    if (panel) {
      dockview.removePanel(panel);
    }
  }
  try {
    await removePanelFromAllProjects(panelId);
  } catch {
    // Best-effort; see above.
  }
}

/** "Destroy" on a tab with no separate resource behind it (an app, an ad-hoc
 *  URL tab, a subagent view): the whole action is dropping it from every
 *  project. */
async function executePanelDestroy(panelId: string): Promise<void> {
  await forgetDestroyedPanel(panelId);
  m.redraw();
}

async function executeDestroy(agentId: string, panelId: string): Promise<void> {
  // Destroy the target agent
  try {
    const response = await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/destroy`), {
      method: "POST",
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const detail = (data as { detail?: string }).detail ?? "Unknown error";
      alert(`Failed to destroy agent: ${detail}`);
      return;
    }
  } catch (e) {
    alert(`Failed to destroy agent: ${(e as Error).message}`);
    return;
  }

  // Remove from local state
  removeAgentLocally(agentId);

  await forgetDestroyedPanel(panelId);

  m.redraw();
}

/** Reflect a live tmux session change onto the owning terminal tab. Matches by
 *  ``terminalId`` when a client switched sessions, or by the immutable
 *  ``session_id`` when a session was renamed (``terminalId`` null). Records the
 *  ``session_id`` on first sight so later rename events can find the tab. */
function handleTerminalSessionUpdate(terminalId: string | null, sessionId: string, sessionName: string): void {
  if (!dockview) return;
  let targetPanelId: string | null = null;
  for (const [panelId, params] of panelParams) {
    if (terminalId !== null) {
      if (params.terminalId === terminalId) {
        targetPanelId = panelId;
        break;
      }
    } else if (params.terminalSessionId === sessionId) {
      targetPanelId = panelId;
      break;
    }
  }
  if (targetPanelId === null) return;
  const params = panelParams.get(targetPanelId);
  if (!params) return;
  params.terminalSessionName = sessionName;
  params.terminalSessionId = sessionId;
  params.title = sessionName;
  dockview.panels.find((p) => p.id === targetPanelId)?.api.setTitle(sessionName);
  m.redraw();
  scheduleSave();
}

async function executeTerminalDestroy(sessionName: string, panelId: string): Promise<void> {
  // Kill the tmux session via the terminals API, then drop the tab.
  try {
    const response = await fetch(apiUrl(`/api/terminals/${encodeURIComponent(sessionName)}/destroy`), {
      method: "POST",
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const detail = (data as { detail?: string }).detail ?? "Unknown error";
      alert(`Failed to destroy terminal: ${detail}`);
      return;
    }
  } catch (e) {
    alert(`Failed to destroy terminal: ${(e as Error).message}`);
    return;
  }

  await forgetDestroyedPanel(panelId);

  m.redraw();
}

async function executeBrowserDestroy(name: string, panelId: string): Promise<void> {
  // Retire the browser in the fleet, then drop the tab. Closing the tab alone
  // only detaches the pane; this frees the browser (and forgets its profile),
  // symmetric to destroying an agent or a terminal. Routed through the shell's
  // same-origin ``DELETE /api/browsers/<name>`` passthrough because the browser
  // daemon lives on its own service origin (no CORS for a direct cross-origin
  // fetch); the passthrough forwards to the daemon's ``DELETE /browsers/<name>``.
  try {
    const response = await fetch(apiUrl(`/api/browsers/${encodeURIComponent(name)}`), {
      method: "DELETE",
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const detail = (data as { error?: string }).error ?? "Unknown error";
      alert(`Failed to destroy browser: ${detail}`);
      return;
    }
  } catch (e) {
    alert(`Failed to destroy browser: ${(e as Error).message}`);
    return;
  }

  await forgetDestroyedPanel(panelId);

  m.redraw();
}

export const DockviewWorkspace: m.Component = {
  oncreate(vnode: m.VnodeDOM) {
    const wrapper = vnode.dom as HTMLElement;
    initializeDockview(wrapper);
  },

  onupdate(_vnode: m.VnodeDOM) {
    // Resize the dockview when the container changes
    if (dockview && dockviewContainer) {
      requestAnimationFrame(() => {
        if (dockviewContainer) {
          const rect = dockviewContainer.getBoundingClientRect();
          dockview!.layout(rect.width, rect.height);
        }
      });
    }
  },

  view() {
    return m(
      "div",
      {
        class: "dockview-workspace",
        style: "width: 100%; height: 100%;",
      },
      [
        showNewChatModal
          ? m(CreateAgentModal, {
              mode: "chat",
              onCreated(newAgentId: string, newAgentName: string) {
                showNewChatModal = false;
                const targetGroup = newTabTargetGroup;
                newTabTargetGroup = null;
                focusOrCreateChatPanel(newAgentId, newAgentName, targetGroup);
              },
              onCancel() {
                showNewChatModal = false;
                newTabTargetGroup = null;
              },
            })
          : null,

        showNewAgentModal
          ? m(CreateAgentModal, {
              mode: "worktree",
              onCreated(newAgentId: string, newAgentName: string) {
                showNewAgentModal = false;
                const targetGroup = newTabTargetGroup;
                newTabTargetGroup = null;
                focusOrCreateChatPanel(newAgentId, newAgentName, targetGroup);
              },
              onCancel() {
                showNewAgentModal = false;
                newTabTargetGroup = null;
              },
            })
          : null,

        showNewBrowserModal
          ? m(CreateBrowserModal, {
              // NO `key` here. This modal sits in a children array among unkeyed
              // sibling vnodes (the other modals/dialogs); Mithril throws "vnodes must
              // either all have keys or none" if one child is keyed and the rest aren't,
              // which silently kills the entire render so the modal never appears. A key
              // isn't needed anyway: onAccept sets showNewBrowserModal=false before the
              // POST, so a failure re-open (showNewBrowserModal back to true) is a fresh
              // mount and oninit re-reads initialName/initialError on its own.
              // Names of browsers already in the fleet, so the modal can
              // pre-validate a typed name and reject a duplicate inline BEFORE
              // opening a pane or calling create -- never optimistically
              // touching the pane of the browser that already owns that name.
              existingBrowserNames: browserFleet.map((b) => b.id),
              // Set only when re-opened after a background create failed: the
              // modal pre-fills the input with this name and shows the error
              // inline (instead of fetching a fresh random name).
              initialName: newBrowserPrefillName ?? undefined,
              initialError: newBrowserError,
              // Fires the instant the user accepts a name: open the optimistic
              // 'starting' pane (which shows the full "Starting browser…" overlay
              // and flips to the live page on its own when the daemon broadcasts
              // ``running``) AND close the modal immediately -- we don't wait for
              // the create POST. Returns whether THIS call created a new pane (vs
              // deduped onto an existing one) so a later failure only tears down a
              // pane this flow created. ``newTabTargetGroup`` is cleared here too
              // since the modal is done; the background POST's success/failure
              // callbacks reference the pane by name, not the group.
              onAccept(browserName: string): boolean {
                const createdPane = openBrowserSessionTab(browserName, newTabTargetGroup);
                showNewBrowserModal = false;
                newTabTargetGroup = null;
                // The accept succeeded optimistically; clear any leftover failure
                // pre-fill so a subsequent clean open starts fresh.
                newBrowserPrefillName = null;
                newBrowserError = null;
                return createdPane;
              },
              // The background create POST succeeded: the modal is already closed
              // and the pane already open, so just refresh the fleet so the next
              // dropdown lists the new browser.
              onCreated() {
                refreshBrowserFleet();
              },
              // Create failed (400 invalid / 409 duplicate-or-full / 503
              // installing / network). Two things must happen so the user always
              // learns WHY the browser didn't open: (1) tear down the optimistic
              // pane ONLY if this flow created it (``createdPane``) -- if the open
              // deduped onto a pre-existing browser's healthy pane, leave it
              // alone; and (2) RE-OPEN this modal pre-filled with the typed name
              // and the daemon's ``reason`` shown inline, so the failure is
              // surfaced rather than the pane silently vanishing.
              onFailed(browserName: string, createdPane: boolean, reason: string) {
                if (createdPane) {
                  closeBrowserSessionTab(browserName);
                }
                newBrowserPrefillName = browserName;
                newBrowserError = reason;
                showNewBrowserModal = true;
                m.redraw();
              },
              onCancel() {
                showNewBrowserModal = false;
                newTabTargetGroup = null;
                newBrowserPrefillName = null;
                newBrowserError = null;
              },
            })
          : null,

        showDestroyDialog && destroyTargetAgentId && destroyTargetAgentName
          ? m(DestroyConfirmDialog, {
              agentName: destroyTargetAgentName,
              details: DESTROY_CHAT_DETAILS,
              onConfirm() {
                showDestroyDialog = false;
                const targetId = destroyTargetAgentId!;
                const panelId = destroyTargetPanelId!;
                destroyTargetAgentId = null;
                destroyTargetAgentName = null;
                destroyTargetPanelId = null;
                executeDestroy(targetId, panelId);
              },
              onCancel() {
                showDestroyDialog = false;
                destroyTargetAgentId = null;
                destroyTargetAgentName = null;
                destroyTargetPanelId = null;
              },
            })
          : null,

        showTerminalDestroyDialog && terminalDestroySessionName
          ? m(DestroyConfirmDialog, {
              agentName: terminalDestroySessionName,
              title: "Destroy terminal",
              details: DESTROY_TERMINAL_DETAILS,
              onConfirm() {
                showTerminalDestroyDialog = false;
                const sessionName = terminalDestroySessionName!;
                const panelId = terminalDestroyPanelId!;
                terminalDestroySessionName = null;
                terminalDestroyPanelId = null;
                executeTerminalDestroy(sessionName, panelId);
              },
              onCancel() {
                showTerminalDestroyDialog = false;
                terminalDestroySessionName = null;
                terminalDestroyPanelId = null;
              },
            })
          : null,

        showBrowserDestroyDialog && browserDestroyName
          ? m(DestroyConfirmDialog, {
              agentName: browserDestroyName,
              title: "Destroy browser",
              details: DESTROY_BROWSER_DETAILS,
              onConfirm() {
                showBrowserDestroyDialog = false;
                const name = browserDestroyName!;
                const panelId = browserDestroyPanelId!;
                browserDestroyName = null;
                browserDestroyPanelId = null;
                executeBrowserDestroy(name, panelId);
              },
              onCancel() {
                showBrowserDestroyDialog = false;
                browserDestroyName = null;
                browserDestroyPanelId = null;
              },
            })
          : null,

        showPanelDestroyDialog && panelDestroyTitle
          ? m(DestroyConfirmDialog, {
              agentName: panelDestroyTitle,
              title: "Destroy tab",
              details: DESTROY_PANEL_DETAILS,
              onConfirm() {
                showPanelDestroyDialog = false;
                const panelId = panelDestroyPanelId!;
                panelDestroyTitle = null;
                panelDestroyPanelId = null;
                void executePanelDestroy(panelId);
              },
              onCancel() {
                showPanelDestroyDialog = false;
                panelDestroyTitle = null;
                panelDestroyPanelId = null;
              },
            })
          : null,

        showShareModal && shareServiceName
          ? m(ShareModal, {
              serviceName: shareServiceName,
              onClose() {
                showShareModal = false;
                shareServiceName = null;
              },
            })
          : null,
      ],
    );
  },
};
