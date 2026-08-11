/**
 * The dock: one DockviewComponent holding the tabs of whichever *view* is
 * mounted, plus the bookkeeping that ties those tabs to the machine.
 *
 * A view is either a project (a filter over the machine's objects plus its own
 * saved arrangement) or Everything (the unfiltered view, and the home). Both
 * mount here identically -- switching saves the outgoing arrangement and loads
 * the incoming one -- and the only difference is membership: a project has an
 * explicit member list, and Everything has none because it shows whatever the
 * machine holds.
 *
 * Membership is many-to-many and nothing owns anything, so the rules the dock
 * enforces are narrow:
 *   - opening an object in a project adds it to that project's member list;
 *   - closing a tab changes no membership and stops nothing -- the object keeps
 *     running, backgrounded, and the sidebar keeps listing it;
 *   - destroying is the one thing that reaches across views: the object is gone
 *     from the machine, so it leaves every project's saved content and member
 *     list at once.
 *
 * The dock is never empty. A view with no panels gets a New Tab launcher, which
 * is also what the "+" opens and what a freshly-created project lands on.
 */

import m from "mithril";
import {
  DockviewComponent,
  themeLight,
  type DockviewGroupPanel,
  type IContentRenderer,
  type IHeaderActionsRenderer,
  type ITabRenderer,
  type SerializedDockview,
  type TabPartInitParameters,
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
import { pickableApps } from "./AllAppsPicker";
import { NewTabLauncher, buildLauncherRows } from "./NewTabLauncher";
import type { LaunchKind, LauncherRow } from "./NewTabLauncher";
import { placeMenu } from "./Sidebar";
import type { MenuAnchor, QuickAddTabType, SidebarTabRow } from "./Sidebar";
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
  addMember,
  autosaveProject,
  buildEverythingMembers,
  chooseInitialViewId,
  fetchProjectContent,
  fetchProjectsList,
  isEverythingView,
  memberKindFromRef,
  memberRef,
  projectForViewId,
  removeMember,
  removePanelFromAllProjects,
  shareMember,
  type MachineInventory,
  type MachineObject,
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
// New Tab launcher panels. The prefix is what tells a launcher apart after a
// layout restore, when all that survives is the panel id and its params.
const LAUNCHER_PANEL_ID_PREFIX = "new-tab-";
const LAUNCHER_PANEL_TITLE = "New tab";

// The per-workspace browser fleet's service name. Its panes are the one kind
// addressed per session rather than per service, so several places have to tell
// it apart from an ordinary app.
const BROWSER_SERVICE_NAME = "browser";

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

type PanelType = "chat" | "iframe" | "subagent" | "launcher";

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

// The dockview group whose "+" (or whose launcher tab) opened the New chat /
// New agent / New browser modal. Captured at click time because those modals
// create their panel asynchronously (after the user confirms), by which point
// the active group may have changed. Consumed in the modal's onCreated
// callback so the new tab lands in the pane it was asked for, then cleared.
let newTabTargetGroup: DockviewGroupPanel | null = null;
// The launcher tab the pending modal was started from, if any. A launcher is
// the question "what do you want in this pane", so answering it replaces the
// launcher rather than leaving it behind -- but only once the answer actually
// arrives, which for these modals is after the user confirms.
let newTabSourceLauncherPanelId: string | null = null;

// Second paragraph of each destroy confirmation. Destroying is not a louder
// Close: closing a tab leaves the object running and still filed wherever it
// was filed, while destroying takes it off the machine -- so it leaves every
// project at once, and each variant says so outright. The chat variant also
// says the transcript survives, because that is the thing people are actually
// afraid of losing.
const DESTROY_CHAT_DETAILS =
  "The agent is removed from every project that shows it, not just this one. The transcript stays accessible.";
const DESTROY_TERMINAL_DETAILS =
  "The tmux session is killed and the terminal is removed from every project that shows it, not just this one.";
const DESTROY_BROWSER_DETAILS =
  "The browser is retired from the fleet and removed from every project that shows it, not just this one.";

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
// True while a view's content is being mounted. The teardown half of that
// removes every panel one at a time, which must not be mistaken for the user
// emptying the dock (see ``ensureDockIsNotEmpty``).
let isApplyingLayout = false;

// ---------- Active-view state ----------

// Cached project registry: the sidebar's project list and member lists, and the
// display names in the project-was-deleted notice. Everything is never in here
// -- it has no registry entry. Refreshed at startup and on every project
// broadcast, membership changes included.
let availableProjects: ProjectInfo[] = [];
// The view whose content is currently mounted in the dockview, and so also the
// view autosave writes to -- a project id, or EVERYTHING_VIEW_ID. Deliberately
// distinct from the *chosen* view in ClientIdentity, which the switcher
// persists the moment the user clicks -- before this module has loaded
// anything: saving against the chosen id would write the outgoing view's
// arrangement into the incoming one, and the already-on-it guard in
// ``switchToView`` would read a pick that recorded its choice first as a no-op
// and never load it.
let mountedViewId: string | null = null;
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

// ---------- Tabs ----------

// Equal-width tabs (§5 of the design). ``TAB_STRIP_RESERVED_PX`` is the space
// every strip keeps for its "+" and the first tab's leading margin; the ideal
// width is what is left over, divided by the tabs, and clamped so a strip full
// of tabs stays readable and a strip holding one does not stretch it across the
// pane.
const TAB_STRIP_RESERVED_PX = 44;
const TAB_WIDTH_MIN_PX = 100;
const TAB_WIDTH_MAX_PX = 220;
// A title too long for its tab fades out over its last 20px instead of taking
// an ellipsis: the tail of a truncated name is more legible than "...", and the
// fade never appears on a title that fits.
const TAB_TITLE_FADE_PX = 20;

/** One tab strip's contribution to the shared tab width. */
export interface TabStripMetrics {
  // Width of the whole header, "+" included -- what TAB_STRIP_RESERVED_PX is
  // measured against.
  width: number;
  tabCount: number;
}

/**
 * The one width every tab in every strip renders at.
 *
 * Per strip the ideal is what is left of its width once the "+" is accounted
 * for, shared between its tabs; the narrowest of those ideals wins, so no strip
 * ends up scrolling while another has room to spare. The result is clamped to
 * [100, 220]: below the floor a tab shows no usable title, and above the
 * ceiling a lone tab looks like a mistake. Strips with no tabs are skipped, and
 * a dock with no tabs at all answers with the ceiling -- there is nothing to
 * apply it to.
 */
export function equalTabWidth(strips: readonly TabStripMetrics[]): number {
  let ideal = Number.POSITIVE_INFINITY;
  for (const strip of strips) {
    if (strip.tabCount <= 0) continue;
    ideal = Math.min(ideal, (strip.width - TAB_STRIP_RESERVED_PX) / strip.tabCount);
  }
  if (!Number.isFinite(ideal)) return TAB_WIDTH_MAX_PX;
  return Math.round(Math.min(TAB_WIDTH_MAX_PX, Math.max(TAB_WIDTH_MIN_PX, ideal)));
}

/**
 * Whether a title actually overflows the box it is drawn in.
 *
 * Only a truncated title gets the trailing fade; a short one stays crisp to its
 * last pixel. The one-pixel tolerance is for sub-pixel layout, where a title
 * that fits exactly can measure a fraction wider than its box.
 */
export function isTitleTruncated(scrollWidth: number, clientWidth: number): boolean {
  return scrollWidth > clientWidth + 1;
}

const XMLNS = "http://www.w3.org/2000/svg";

// Inner markup for the tab's kind glyphs, drawn on the same 24x24 Feather grid
// as `icons.ts`, which carries none of them. The rail and the launcher draw the
// same shapes; all three belong in the shared table once this rework settles.
const TAB_KIND_PATHS = {
  chat:
    '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7' +
    'a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
  browser:
    '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/>' +
    '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  app: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>',
  url:
    '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>' +
    '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  launcher: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  // Filled rather than stroked: at 14px a 1px-radius ring reads as fuzz.
  kebab:
    '<circle cx="12" cy="5" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="19" r="1.5" fill="currentColor" stroke="none"/>',
} as const;

type TabIconName = keyof typeof TAB_KIND_PATHS;

/** Full <svg> string for one of the tab strip's own glyphs. */
function tabIcon(name: TabIconName, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${TAB_KIND_PATHS[name]}</svg>`
  );
}

/** What kind of thing a tab is showing, for its leading glyph. Mirrors the
 *  member-ref classification -- browsers and terminals are fleets rather than
 *  installed apps and read as their own kinds -- so a tab and its sidebar row
 *  wear the same icon. */
function tabIconNameForPanel(params: PanelParams | undefined): TabIconName {
  if (params === undefined) return "url";
  if (params.panelType === "launcher") return "launcher";
  if (params.panelType === "chat") return "chat";
  if (isTerminalPanelParams(params)) return "terminal";
  if (params.serviceName === BROWSER_SERVICE_NAME) return "browser";
  if (params.serviceName) return "app";
  return "url";
}

// ---------- The tab ⋮ menu ----------

interface TabMenuItem {
  label: string;
  iconName: IconName;
  isDestructive?: boolean;
  run: () => void;
}

// A separator between groups of items; the design puts one above "Close tab".
const MENU_DIVIDER = "divider";

type TabMenuEntry = TabMenuItem | typeof MENU_DIVIDER;

// Menu chrome, settled in the design (§6) and shared with the sidebar's menus:
// a floating card on the primary surface with a hairline border, 8px radius and
// the overlay elevation shadow, holding 32px rows of icon + label.
const TAB_MENU_CARD_CLASS = "fixed z-50 min-w-[180px] rounded-lg border border-border bg-surface py-1 text-[13px]";
const TAB_MENU_SHADOW_STYLE = "box-shadow: 0 1px 1px 0 rgba(0, 0, 0, 0.08), 0 3px 12px 0 rgba(0, 0, 0, 0.08);";
const TAB_MENU_ROW_CLASS = "flex h-8 w-full cursor-pointer items-center gap-2 px-3 text-left hover:bg-bg-hover";

// The one open tab menu, if any. Only one is ever up: opening another closes
// this one first, and so does an outside press, Escape, a scroll, a resize, or
// picking anything.
let openTabMenu: { close: () => void } | null = null;

function closeTabMenu(): void {
  openTabMenu?.close();
}

/**
 * Open a tab's ⋮ menu against ``anchor``.
 *
 * Built on ``document.body`` rather than inside the tab: the tab strip clips
 * its own overflow (``.dv-tabs-container`` is ``overflow: auto``), so an in-tab
 * menu would be cut off -- the same constraint that puts the hover tooltip on
 * the body. Placement is the sidebar's, so every floating menu in the workspace
 * flips and clamps by one rule. ``onClosed`` lets the tab drop the hover
 * treatment it was holding while its menu was up, and ``trigger`` -- the ⋮ that
 * opened it -- is excluded from the outside-press close so that pressing it
 * again toggles the menu shut instead of closing and reopening it.
 */
function openTabMenuAt(
  anchor: MenuAnchor,
  entries: readonly TabMenuEntry[],
  onClosed: () => void,
  trigger: HTMLElement | null,
): void {
  closeTabMenu();
  const element = document.createElement("div");
  element.className = TAB_MENU_CARD_CLASS;
  element.setAttribute("role", "menu");
  element.style.cssText = `left: 0; top: 0; ${TAB_MENU_SHADOW_STYLE}`;

  const close = (): void => {
    document.removeEventListener("pointerdown", onOutsidePointerDown, true);
    document.removeEventListener("keydown", onKeyDown, true);
    window.removeEventListener("resize", close);
    window.removeEventListener("scroll", close, true);
    element.remove();
    openTabMenu = null;
    onClosed();
  };

  function onOutsidePointerDown(event: PointerEvent): void {
    const target = event.target as Node;
    if (element.contains(target) || trigger?.contains(target) === true) return;
    close();
  }

  function onKeyDown(event: KeyboardEvent): void {
    if (event.key === "Escape") close();
  }

  for (const entry of entries) {
    if (entry === MENU_DIVIDER) {
      const divider = document.createElement("div");
      divider.className = "my-1 border-t border-border";
      element.appendChild(divider);
      continue;
    }
    const row = document.createElement("div");
    row.className = `${TAB_MENU_ROW_CLASS} ${entry.isDestructive ? "text-red-600" : "text-text-primary"}`;
    row.setAttribute("role", "menuitem");
    const glyph = document.createElement("span");
    glyph.className = "flex w-4 shrink-0 items-center justify-center";
    glyph.innerHTML = icon(entry.iconName, { size: 14 });
    const label = document.createElement("span");
    label.className = "min-w-0 flex-1 truncate";
    label.textContent = entry.label;
    row.append(glyph, label);
    row.addEventListener("click", (event) => {
      event.stopPropagation();
      close();
      entry.run();
    });
    element.appendChild(row);
  }

  document.body.appendChild(element);
  // Measured after mounting, as the sidebar's menus and the tooltip are: the
  // card's height depends on how many rows it holds, and both the flip and the
  // clamp need it. This runs before paint, so the card is never seen at the
  // origin.
  const rect = element.getBoundingClientRect();
  const position = placeMenu(
    anchor,
    { width: rect.width, height: rect.height },
    { width: window.innerWidth, height: window.innerHeight },
    "below",
  );
  element.style.left = `${position.left}px`;
  element.style.top = `${position.top}px`;

  document.addEventListener("pointerdown", onOutsidePointerDown, true);
  document.addEventListener("keydown", onKeyDown, true);
  window.addEventListener("resize", close);
  window.addEventListener("scroll", close, true);
  openTabMenu = { close };
}

/**
 * What one tab's ⋮ menu offers, designed per object type against the verbs the
 * backend actually has (§6).
 *
 * Refresh reloads what the tab is showing, which means nothing for a terminal
 * (its tmux session is the content, and reattaching is not a refresh) so that
 * one goes without. Share is an app affordance: the share surface is per
 * registered service. Close tab is always last above the destructive verb,
 * because closing is the safe, membership-preserving one -- the object keeps
 * running and stays filed wherever it was filed.
 *
 * The destructive verb is whatever taking the object off the machine means for
 * its kind: destroying a chat's agent, killing a terminal's tmux session,
 * retiring a browser from the fleet. Apps and ad-hoc pages have none -- nothing
 * here stops a supervised app or deletes its package, and a page is only ever a
 * panel -- so offering one would be a menu item that cannot act.
 */
function tabMenuEntries(panelId: string): TabMenuEntry[] {
  const params = panelParams.get(panelId);
  if (params === undefined) return [];
  const entries: TabMenuEntry[] = [];
  const isTerminal = isTerminalPanelParams(params);
  if (!isTerminal) {
    entries.push({
      label: "Refresh",
      iconName: "refresh",
      run: () => refreshPanelContent(panelId),
    });
  }
  const serviceName = params.serviceName;
  if (serviceName !== undefined && serviceName !== BROWSER_SERVICE_NAME && !isTerminal) {
    entries.push({
      label: `Share ${serviceName}`,
      iconName: "share",
      run: () => {
        shareServiceName = serviceName;
        showShareModal = true;
        m.redraw();
      },
    });
  }
  if (entries.length > 0) entries.push(MENU_DIVIDER);
  entries.push({
    label: "Close tab",
    iconName: "close",
    run: () => {
      const panel = dockview?.panels.find((candidate) => candidate.id === panelId);
      panel?.api.close();
    },
  });
  const destroy = tabDestroyEntry(panelId, params);
  if (destroy !== null) entries.push(destroy);
  return entries;
}

/** The destructive verb for one tab, or null for a kind that has none. Each
 *  raises its own confirmation, which is where what the destroy takes down --
 *  and that it reaches every project, not just this one -- is spelled out. */
function tabDestroyEntry(panelId: string, params: PanelParams): TabMenuItem | null {
  if (params.panelType === "chat") {
    const chatAgentId = params.chatAgentId ?? params.agentId;
    // The primary agent runs the workspace's own services; destroying it would
    // take the machine down, so it is not offered.
    if (!chatAgentId || chatAgentId === getPrimaryAgentId()) return null;
    return {
      label: "Destroy agent",
      iconName: "trash",
      isDestructive: true,
      run: () => {
        destroyTargetAgentId = chatAgentId;
        destroyTargetAgentName = getAgentById(chatAgentId)?.name ?? chatAgentId;
        destroyTargetPanelId = panelId;
        showDestroyDialog = true;
        m.redraw();
      },
    };
  }
  if (isTerminalPanelParams(params)) {
    const sessionName = params.terminalSessionName;
    // A terminal still allocating its tmux session name has no session to kill.
    if (!sessionName) return null;
    return {
      label: "Destroy terminal",
      iconName: "trash",
      isDestructive: true,
      run: () => {
        terminalDestroySessionName = sessionName;
        terminalDestroyPanelId = panelId;
        showTerminalDestroyDialog = true;
        m.redraw();
      },
    };
  }
  if (params.serviceName === BROWSER_SERVICE_NAME) {
    // The browser's name lives in the pane's ``?session=<name>`` query, set on
    // both freshly-opened and layout-restored panes.
    const browserName = browserSessionFromUrl(params.url);
    if (browserName === null) return null;
    return {
      label: "Close browser session",
      iconName: "trash",
      isDestructive: true,
      run: () => {
        browserDestroyName = browserName;
        browserDestroyPanelId = panelId;
        showBrowserDestroyDialog = true;
        m.redraw();
      },
    };
  }
  // An app, an ad-hoc page, or a subagent view: nothing to tear down beyond the
  // panel, and the panel is what Close already handles.
  return null;
}

// ---------- The tab ----------

/** The live tabs, so the width recompute can size them and re-measure their
 *  titles, and so a "+" can flash the launcher its pane already holds. Entries
 *  are added as tabs are rendered and dropped as they are disposed. */
const tabHandlesByPanelId = new Map<string, { element: HTMLElement; refreshTitleFade: () => void }>();

/**
 * One tab: kind glyph, title, then the right-aligned ✕ and ⋮ that hover reveals.
 *
 * The two actions are hidden at rest and shown together (§5) -- a tab at the
 * 100px floor has room for its title or its buttons, not both, and the design
 * asks for the title. They are toggled from here rather than by a CSS
 * ``:hover`` rule because the menu has to hold them open while it is up, after
 * the pointer has left the tab for the menu card.
 */
function createCustomTab(options: { id: string; name: string }): ITabRenderer {
  const element = document.createElement("div");
  element.className = "dv-default-tab dv-custom-tab";

  const kindIcon = document.createElement("span");
  kindIcon.className = "dv-custom-tab-icon";
  kindIcon.style.display = "flex";
  kindIcon.style.flexShrink = "0";
  kindIcon.style.alignItems = "center";
  element.appendChild(kindIcon);

  const content = document.createElement("div");
  content.className = "dv-default-tab-content";
  // The design asks for a trailing fade rather than an ellipsis, so the title
  // clips and the fade (applied below, only when it actually overflows) does
  // the rest.
  content.style.overflow = "hidden";
  content.style.whiteSpace = "nowrap";
  content.style.textOverflow = "clip";
  element.appendChild(content);

  const actions = document.createElement("div");
  actions.className = "dv-custom-tab-actions";
  actions.style.display = "none";
  element.appendChild(actions);

  const disposables: Array<{ dispose: () => void }> = [];
  let isPointerOver = false;
  let isMenuOpen = false;

  /** Fade the title's last 20px, and only while it really is cut off. */
  const refreshTitleFade = (): void => {
    const mask = isTitleTruncated(content.scrollWidth, content.clientWidth)
      ? `linear-gradient(to right, #000 calc(100% - ${TAB_TITLE_FADE_PX}px), transparent 100%)`
      : "";
    content.style.maskImage = mask;
    content.style.webkitMaskImage = mask;
  };

  const updateActionsVisibility = (): void => {
    actions.style.display = isPointerOver || isMenuOpen ? "flex" : "none";
    // Revealing the buttons takes room from the title, so the fade is re-judged
    // against the box the title actually has now.
    refreshTitleFade();
  };

  return {
    element,
    init(parameters: TabPartInitParameters) {
      content.textContent = parameters.title ?? "";
      kindIcon.innerHTML = tabIcon(tabIconNameForPanel(panelParams.get(options.id)), 14);
      disposables.push(
        parameters.api.onDidTitleChange((event) => {
          content.textContent = event.title ?? "";
          refreshTitleFade();
        }),
      );

      const params = panelParams.get(options.id);
      const isLauncher = params?.panelType === "launcher";

      if (params?.panelType === "chat") {
        appendChatLivenessDot(element, params.chatAgentId ?? params.agentId, disposables);
      }

      actions.appendChild(
        createTabActionButton("Close tab", "close", disposables, () => {
          parameters.api.close();
        }),
      );

      // A launcher tab is a question about this pane, not an object: there is
      // nothing to refresh, share or destroy, so it carries only ✕ (§5).
      if (!isLauncher) {
        const openMenu = (anchor: MenuAnchor, trigger: HTMLElement | null): void => {
          if (isMenuOpen) {
            closeTabMenu();
            return;
          }
          isMenuOpen = true;
          updateActionsVisibility();
          openTabMenuAt(
            anchor,
            tabMenuEntries(options.id),
            () => {
              isMenuOpen = false;
              updateActionsVisibility();
            },
            trigger,
          );
        };
        const menuButton = createTabActionButton("Tab options", "kebab", disposables, () => {
          openMenu(menuButton.getBoundingClientRect(), menuButton);
        });
        actions.appendChild(menuButton);
        element.addEventListener("contextmenu", (event: MouseEvent) => {
          event.preventDefault();
          // Anchored on the pointer so the menu opens where the click landed.
          openMenu(
            { left: event.clientX, right: event.clientX, top: event.clientY, bottom: event.clientY, width: 0 },
            null,
          );
        });
      }

      element.addEventListener("mouseenter", () => {
        isPointerOver = true;
        updateActionsVisibility();
      });
      element.addEventListener("mouseleave", () => {
        isPointerOver = false;
        updateActionsVisibility();
      });
      updateActionsVisibility();
      tabHandlesByPanelId.set(options.id, { element, refreshTitleFade });
    },
    dispose() {
      tabHandlesByPanelId.delete(options.id);
      for (const d of disposables) {
        d.dispose();
      }
      disposables.length = 0;
    },
  };
}

/**
 * Add the per-agent liveness dot to a chat tab.
 *
 * Distinct from the chat's activity indicator: this tracks the agent's mngr
 * lifecycle state -- green while its claude process is working (RUNNING),
 * yellow while it is idle and waiting on the user (WAITING), grey while it is
 * dormant (DONE/STOPPED/etc.; revives on the next message). Hovering shows the
 * exact lifecycle state via a body-level tooltip (a native ``title`` is
 * suppressed on dockview's draggable tabs -- see ``attachHoverTooltip``).
 * Hidden until the agent's state is known.
 *
 * The lifecycle RUNNING/WAITING split comes only from the backend's lifecycle
 * poll and lags a sent message, so the color is resolved through
 * ``effectiveLifecycleState`` against the prompt activity signal
 * (transcript-derived, plus the optimistic forced-THINKING the send applies).
 * That makes the dot turn green the instant a message is sent, in step with the
 * activity indicator -- hence the second listener below on the activity
 * overlay, since an optimistic send is not a WS update.
 */
function appendChatLivenessDot(
  element: HTMLElement,
  chatAgentId: string,
  disposables: Array<{ dispose: () => void }>,
): void {
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
    // ``data-liveness`` drives the color (the primary signal). Several lifecycle
    // states share a color (DONE/STOPPED/REPLACED/UNKNOWN are all grey
    // "dormant"; RUNNING/RUNNING_UNKNOWN_AGENT_TYPE are both green), so
    // ``data-lifecycle-state`` carries the exact state and the CSS gives each a
    // subtly different circular treatment (solid / ring / ring-with-dot /
    // faded) so same-color states stay tellable apart.
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
}

/** One of a tab's two hover-revealed buttons. The pointerdown guard keeps a
 *  click on the button from starting dockview's tab drag or activating the
 *  tab. */
function createTabActionButton(
  title: string,
  iconName: IconName | "kebab",
  disposables: Array<{ dispose: () => void }>,
  onClick: (ev: MouseEvent) => void,
): HTMLButtonElement {
  const button = document.createElement("button");
  button.className = "dv-custom-tab-action";
  button.setAttribute("aria-label", title);
  // No explicit size: `.dv-custom-tab-action svg` sizes these to 12px in CSS.
  button.innerHTML = iconName === "kebab" ? tabIcon("kebab", 14) : icon(iconName);
  const tooltip = attachHoverTooltip(button);
  tooltip.setText(title);
  disposables.push(tooltip);
  button.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
  });
  button.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    onClick(ev);
  });
  return button;
}

// ---------- Equal-width tabs ----------

// Coalesces every trigger -- layout change, pane resize, window resize -- into
// one recompute per frame: several of them fire together on a single splitter
// drag, and re-measuring per event would read a half-applied layout.
let tabWidthFrame: number | null = null;
// Watches each tab strip so dragging a splitter (which resizes panes without
// changing the layout) re-fits the tabs. Strips come and go with groups, so the
// observed set is reconciled on every recompute -- only genuinely new strips are
// observed (each observe delivers an initial notification, so re-observing the
// same one every frame would recompute forever) and disposed ones are dropped
// rather than held alive by the observer.
let tabStripObserver: ResizeObserver | null = null;
const observedTabStrips = new Set<HTMLElement>();

/** Point the resize observer at exactly the strips that exist now. */
function observeTabStrips(headers: readonly HTMLElement[]): void {
  if (tabStripObserver === null) return;
  const current = new Set(headers);
  for (const observed of observedTabStrips) {
    if (current.has(observed)) continue;
    tabStripObserver.unobserve(observed);
    observedTabStrips.delete(observed);
  }
  for (const header of headers) {
    if (observedTabStrips.has(header)) continue;
    tabStripObserver.observe(header);
    observedTabStrips.add(header);
  }
}

/** Every tab strip currently in the dock, header and all. */
function tabStripHeaders(): HTMLElement[] {
  if (dockviewContainer === null) return [];
  return Array.from(dockviewContainer.querySelectorAll<HTMLElement>(".dv-tabs-and-actions-container"));
}

/** Re-fit every tab to the one shared width, and re-judge each title's fade
 *  against the box it ends up with. */
function recomputeTabWidths(): void {
  const headers = tabStripHeaders();
  if (headers.length === 0) return;
  observeTabStrips(headers);
  const metrics: TabStripMetrics[] = [];
  const tabElements: HTMLElement[] = [];
  for (const header of headers) {
    const tabs = Array.from(header.querySelectorAll<HTMLElement>(".dv-tabs-container > .dv-tab"));
    metrics.push({ width: header.clientWidth, tabCount: tabs.length });
    tabElements.push(...tabs);
  }
  const width = `${equalTabWidth(metrics)}px`;
  for (const tab of tabElements) {
    tab.style.width = width;
  }
  for (const handle of tabHandlesByPanelId.values()) {
    handle.refreshTitleFade();
  }
}

/** Ask for a recompute on the next frame. Safe to call from anywhere that
 *  might have changed a strip's width or its tab count. */
function scheduleTabWidthRecompute(): void {
  if (tabWidthFrame !== null) return;
  tabWidthFrame = requestAnimationFrame(() => {
    tabWidthFrame = null;
    recomputeTabWidths();
  });
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

/** Placement options that tab a newly-added panel into ``targetGroup`` (the
 *  pane whose "+" was clicked, or whose launcher asked for it) instead of
 *  letting dockview fall back to the currently-active group. Returns an empty
 *  object -- i.e. default placement -- when no target is given (the dock had no
 *  pane yet) or the target group has since been disposed (e.g. it was closed
 *  while a New chat / New agent modal was open). */
function placementForGroup(targetGroup: DockviewGroupPanel | null | undefined): AddPanelPlacementOptions {
  if (targetGroup && dockview?.groups.some((g) => g.id === targetGroup.id)) {
    return { position: { referenceGroup: targetGroup.id } };
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

// Cached snapshot of the browser fleet, re-read whenever a view is mounted, a
// launcher opens, or a browser is created, so it reflects the fleet as it is
// rather than as it was at boot. It is what the sidebar's tab list and the
// launcher's tables enumerate browsers from.
//
// Note: we no longer gate the "New browser" button on the daemon's
// ``can_create``. A create is accepted even during startup/restore (it queues
// behind the serialized restore on the daemon's shared launch lock) and the
// fleet cap / duplicate-name rejections come back as inline errors in the
// New-browser modal, so the button stays always clickable.
let browserFleet: BrowserInfo[] = [];

/** Fetch the live browser fleet into the cache. ``onUpdate`` runs after the
 *  cache is refreshed, so a surface built synchronously from it (the sidebar
 *  list, an open launcher) can repaint with what the async fetch returned
 *  rather than showing a stale fleet until something else redraws it. */
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
// document. Interactive callers (the launcher, browser create) only run in a
// browser.
if (typeof document !== "undefined") {
  refreshBrowserFleet();
}

/** Open (or focus, via ``addPanelForRef`` dedup) the pane for browser
 *  ``name``. Routed through the same ``service:browser?session=<name>`` ref the
 *  agent CLI uses so the two surfaces share dedup/focus and on-disk shape.
 *  If the pane is already open, ``addPanelForRef`` focuses it; opening a new
 *  pane activates it (the user explicitly asked for this browser, so taking
 *  focus is the intended behavior). Tabs into ``targetGroup`` when it's a live
 *  group.
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
  addPanelForRef(`service:browser?session=${name}`, getPrimaryAgentId(), placementForGroup(targetGroup));
  // The fleet gained (or is about to gain) a browser the sidebar lists.
  refreshMachineInventory();
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

// ---------- The "+" and the New Tab launcher ----------

/** The group a panel currently lives in, or null when it is not open. */
function groupForPanel(panelId: string): DockviewGroupPanel | null {
  return dockview?.panels.find((panel) => panel.id === panelId)?.api.group ?? null;
}

/** The launcher already open in ``group``, or null. At most one launcher lives
 *  in a pane: a second would ask the same question twice. */
function launcherPanelIdInGroup(group: DockviewGroupPanel | null): string | null {
  if (!dockview || group === null) return null;
  for (const panel of dockview.panels) {
    if (panelParams.get(panel.id)?.panelType !== "launcher") continue;
    if (panel.api.group.id === group.id) return panel.id;
  }
  return null;
}

/** Blink a tab to point at it. Used when the "+" is clicked in a pane that
 *  already has a launcher: the answer is "it is right there", and opening a
 *  second one would only clutter the strip. */
function flashPanelTab(panelId: string): void {
  const handle = tabHandlesByPanelId.get(panelId);
  handle?.element.animate([{ opacity: 1 }, { opacity: 0.3 }, { opacity: 1 }], { duration: 240, iterations: 2 });
}

/**
 * Open a New Tab launcher in ``targetGroup`` (or wherever dockview puts it when
 * there is no group yet), focusing and flashing the one already there instead
 * of stacking a second.
 *
 * A launcher is not an object on the machine, so it joins no member list. It is
 * what the "+" opens, what a view with no saved content mounts, and what closing
 * the last tab leaves behind -- the dock is never empty.
 */
function openLauncherPanel(targetGroup: DockviewGroupPanel | null): string | null {
  if (!dockview) return null;
  const existingPanelId = launcherPanelIdInGroup(targetGroup);
  if (existingPanelId !== null) {
    const existing = dockview.panels.find((panel) => panel.id === existingPanelId);
    if (existing) dockview.setActivePanel(existing);
    flashPanelTab(existingPanelId);
    return existingPanelId;
  }
  const panelId = mintId(LAUNCHER_PANEL_ID_PREFIX);
  const params: PanelParams = { panelType: "launcher", agentId: getPrimaryAgentId() };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "launcher",
    title: LAUNCHER_PANEL_TITLE,
    params,
    ...placementForGroup(targetGroup),
  });
  // The launcher lists the machine, so the fleets it lists are re-read as it
  // opens rather than only at startup.
  refreshMachineInventory();
  return panelId;
}

/** Retire the launcher a just-opened tab was asked for from. The launcher asks
 *  "what do you want in this pane", so an answer replaces it -- but only once
 *  the answer has actually arrived, which for the naming modals is after the
 *  user confirms. */
function retireLauncher(panelId: string | null): void {
  if (panelId === null || !dockview) return;
  if (panelParams.get(panelId)?.panelType !== "launcher") return;
  const panel = dockview.panels.find((candidate) => candidate.id === panelId);
  if (panel) dockview.removePanel(panel);
}

/** Keep the dock from ever being empty: the view a user emptied gets a
 *  launcher, which is the design's empty state. Suppressed while a layout is
 *  being mounted, where the teardown legitimately removes every panel. */
function ensureDockIsNotEmpty(): void {
  if (isApplyingLayout || !dockview) return;
  if (dockview.panels.length > 0) return;
  openLauncherPanel(null);
}

function createAddTabButton(group: DockviewGroupPanel): IHeaderActionsRenderer {
  const element = document.createElement("div");
  element.className = "dockview-add-tab-wrapper";

  const button = document.createElement("button");
  button.className = "dockview-add-tab-button";
  button.setAttribute("aria-label", LAUNCHER_PANEL_TITLE);
  button.textContent = "+";
  const tooltip = attachHoverTooltip(button);
  tooltip.setText(LAUNCHER_PANEL_TITLE);
  element.appendChild(button);

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    openLauncherPanel(group);
  });

  return {
    element,
    init() {},
    dispose() {
      tooltip.dispose();
    },
  };
}

/** Content renderer for a launcher tab. Its attrs are read on every redraw so
 *  the tables follow the machine (an agent appearing, a browser retiring) while
 *  it sits open. */
function createLauncherRenderer(panelId: string): IContentRenderer {
  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  return {
    element,
    init() {
      m.mount(element, {
        view: () =>
          m(NewTabLauncher, {
            rows: launcherRows(),
            memberRefs: activeViewMemberRefs(),
            isEverything: mountedViewId !== null && isEverythingView(mountedViewId),
            onOpenNew: (kind: LaunchKind) => {
              openTabOfTypeInGroup(kind, groupForPanel(panelId), panelId);
            },
            onOpenMember: (row: LauncherRow) => {
              if (openMemberRef(row.ref, groupForPanel(panelId)) !== null) retireLauncher(panelId);
            },
            onOpenFromMachine: (row: LauncherRow) => {
              openFromMachine(row.ref, panelId);
            },
          }),
      });
    },
    dispose() {
      m.mount(element, null);
    },
  };
}

/**
 * Open something the active project does not show yet, from the launcher's
 * "on this machine" table.
 *
 * The object joins this project and is taken from nowhere: membership is
 * many-to-many, so it keeps running and stays in every project that already
 * shows it. Filing comes first so the sidebar lists it even if the panel
 * creation finds nothing to open (a url ref whose page is long gone).
 */
function openFromMachine(ref: string, launcherPanelId: string): void {
  const viewId = mountedViewId;
  const targetGroup = groupForPanel(launcherPanelId);
  void (async () => {
    if (viewId !== null && !isEverythingView(viewId)) {
      try {
        await shareMember(ref, viewId);
        await refreshProjectsList();
      } catch {
        // Opening it is still worth doing; the next open files it again.
      }
    }
    if (openMemberRef(ref, targetGroup) !== null) retireLauncher(launcherPanelId);
    m.redraw();
  })();
}

/** Every object on the machine, as launcher rows. The machine reports no
 *  per-object last-activity yet, so the recency column reads as unknown rather
 *  than being invented here. */
function launcherRows(): LauncherRow[] {
  return buildLauncherRows(machineInventory(), {});
}

/** The active view's member refs, which the launcher splits the machine
 *  against. Empty for Everything, which has no member list -- and which the
 *  launcher renders as one machine-wide table instead of a split. */
function activeViewMemberRefs(): string[] {
  const viewId = mountedViewId;
  if (viewId === null || isEverythingView(viewId)) return [];
  return projectForViewId(availableProjects, viewId)?.members ?? [];
}

// ---------- The machine, as the sidebar and the launcher see it ----------

/** Re-read the fleets the sidebar and the launcher enumerate. Called when a
 *  view is mounted, when a launcher opens, and after creating a browser or a
 *  terminal, so a freshly-created one is listed without waiting for the next
 *  mount. */
function refreshMachineInventory(): void {
  refreshBrowserFleet(() => m.redraw());
  refreshTerminalFleet(() => m.redraw());
}

/**
 * Everything the machine currently holds, gathered per kind from the source
 * that knows about it.
 *
 * This is what makes Everything the *home*: its tab list enumerates the machine
 * rather than unioning the projects' member lists, so an object filed in no
 * project at all still shows up. Names are the identity each kind's ref is
 * built from -- a chat's stable agent id, a tmux session, a fleet browser's
 * session name, a service name -- and never the display label.
 */
function machineInventory(): MachineInventory {
  const urlTabs: MachineObject[] = [];
  for (const [panelId, ref] of memberRefByPanelId) {
    if (memberKindFromRef(ref) !== "url") continue;
    urlTabs.push({ name: memberRefBody(ref), label: panelParams.get(panelId)?.title ?? "Page" });
  }
  return {
    chatAgents: getAgents().map((agent) => ({ name: agent.id, label: agent.name })),
    terminals: terminalFleet.map((terminal) => ({ name: terminal.session_name, label: terminal.session_name })),
    browsers: browserFleet.map((browser) => ({ name: browser.id, label: `Browser ${browser.id}` })),
    apps: pickableApps().map((app) => ({ name: app.name, label: app.name })),
    urlTabs,
  };
}

/** What a member ref is called in the sidebar. A chat is filed under its agent
 *  id, so its label is looked up live and follows a rename; everything else is
 *  named by the identity it is filed under. An ad-hoc page has no name of its
 *  own beyond its tab's title, which only exists while it is open. */
function labelForMemberRef(ref: string): string {
  const body = memberRefBody(ref);
  switch (memberKindFromRef(ref)) {
    case "chat":
      return getAgentById(body)?.name ?? getProtoAgents().find((proto) => proto.agent_id === body)?.name ?? body;
    case "terminal":
      return body;
    case "browser":
      return `Browser ${serviceSessionLabel(parseServiceRefBody(body).query)}`;
    case "app":
      return body;
    case "url": {
      const panelId = panelIdForMemberRef(ref);
      return (panelId === null ? undefined : panelParams.get(panelId)?.title) ?? "Page";
    }
  }
}

/** The project registry, for the sidebar's switcher and its member lists.
 *  Everything is never in it. */
export function getAvailableProjects(): ProjectInfo[] {
  return availableProjects;
}

/** The mounted view: a project id, or EVERYTHING_VIEW_ID. Empty only before the
 *  registry has loaded. */
export function getActiveViewId(): string {
  return mountedViewId ?? "";
}

/** Re-read the project registry. Exported for the sidebar, whose create,
 *  rename and delete all change it. */
export function refreshProjects(): void {
  void refreshProjectsList();
}

/**
 * The active view's tab list: every object it holds, open or backgrounded.
 *
 * A project lists its members -- explicitly filed, so a member with no panel is
 * simply backgrounded and stays listed. Everything lists the machine, because
 * an object filed in no project at all has to appear somewhere and that
 * somewhere is the home.
 */
export function getSidebarRows(): SidebarTabRow[] {
  const viewId = mountedViewId;
  if (viewId === null) return [];
  if (isEverythingView(viewId)) {
    return buildEverythingMembers(machineInventory(), {}).map((row) => ({
      ref: row.ref,
      kind: row.kind,
      label: row.label,
      isOpen: panelIdForMemberRef(row.ref) !== null,
    }));
  }
  const members = projectForViewId(availableProjects, viewId)?.members ?? [];
  return members.map((ref) => ({
    ref,
    kind: memberKindFromRef(ref),
    label: labelForMemberRef(ref),
    isOpen: panelIdForMemberRef(ref) !== null,
  }));
}

/**
 * Focus the tab a member already has, or open one for it in the active pane.
 *
 * Returns the panel id, or null when the object cannot be opened from its ref
 * alone: an ad-hoc page is addressed by a hash of its panel, so once its tab is
 * closed the URL it pointed at is gone and only "Remove from project" is left
 * for it.
 */
function openMemberRef(ref: string, targetGroup: DockviewGroupPanel | null): string | null {
  if (!dockview) return null;
  const openPanelId = panelIdForMemberRef(ref);
  if (openPanelId !== null) {
    const panel = dockview.panels.find((candidate) => candidate.id === openPanelId);
    if (panel) dockview.setActivePanel(panel);
    return openPanelId;
  }
  const body = memberRefBody(ref);
  switch (memberKindFromRef(ref)) {
    case "chat": {
      const chatAgentId = body;
      focusOrCreateChatPanel(chatAgentId, getAgentById(chatAgentId)?.name ?? chatAgentId, targetGroup);
      return chatPanelId(chatAgentId);
    }
    case "terminal":
      return addTerminalPanel(body, { targetGroup });
    case "browser":
    case "app":
      // Both are ``service:`` refs, which addPanelForRef creates and dedups --
      // including the browser fleet's ``?session=`` form.
      return addPanelForRef(ref, getPrimaryAgentId(), placementForGroup(targetGroup));
    case "url":
      console.warn(`Cannot reopen ${ref}: an ad-hoc page's address does not survive its tab`);
      return null;
  }
}

/** Sidebar row click: focus the object's tab, or open it into the active pane. */
export function openMemberRow(row: SidebarTabRow): void {
  openMemberRef(row.ref, null);
  m.redraw();
}

/**
 * Stop showing a member in the active view.
 *
 * This hides it here and nowhere else: it keeps running, it stays in every
 * other project showing it, and it stays in Everything. Its tab goes with it --
 * a view that no longer shows an object must not keep it docked -- which is the
 * one place closing a tab and removing a member coincide, and only because the
 * removal drove it.
 */
export function removeMemberRow(row: SidebarTabRow): void {
  const viewId = mountedViewId;
  // Nothing can be removed from Everything: it is the home, and an object
  // leaves it only by being destroyed.
  if (viewId === null || isEverythingView(viewId)) return;
  const panelId = panelIdForMemberRef(row.ref);
  if (panelId !== null && dockview) {
    const panel = dockview.panels.find((candidate) => candidate.id === panelId);
    if (panel) dockview.removePanel(panel);
  }
  void (async () => {
    try {
      await removeMember(viewId, row.ref);
      await refreshProjectsList();
    } catch (e) {
      alert(`Failed to remove from project: ${(e as Error).message}`);
    }
    m.redraw();
  })();
}

/** Open the machine's share surface for an app row. */
export function shareMemberRow(row: SidebarTabRow): void {
  if (row.kind !== "app") return;
  shareServiceName = memberRefBody(row.ref);
  showShareModal = true;
  m.redraw();
}

/**
 * Destroy the object behind a sidebar row, machine-wide.
 *
 * Each kind raises its own confirmation, and each says what it takes down and
 * that the object leaves every project rather than only this view. The panel id
 * handed to the destroy is the live tab's when the object has one, else the
 * deterministic id its kind is always filed under, so destroying something
 * backgrounded still clears the tab another project would otherwise restore.
 */
export function destroyMemberRow(row: SidebarTabRow): void {
  const body = memberRefBody(row.ref);
  const livePanelId = panelIdForMemberRef(row.ref);
  switch (memberKindFromRef(row.ref)) {
    case "chat":
      destroyTargetAgentId = body;
      destroyTargetAgentName = row.label;
      destroyTargetPanelId = livePanelId ?? chatPanelId(body);
      showDestroyDialog = true;
      break;
    case "terminal":
      terminalDestroySessionName = body;
      terminalDestroyPanelId = livePanelId ?? terminalPanelId(body);
      showTerminalDestroyDialog = true;
      break;
    case "browser":
      browserDestroyName = serviceSessionLabel(parseServiceRefBody(body).query);
      // A browser pane's id is minted per open, so a backgrounded one has none
      // to sweep; dropping the member everywhere is the whole of it.
      browserDestroyPanelId = livePanelId ?? row.ref;
      showBrowserDestroyDialog = true;
      break;
    // An app or an ad-hoc page has nothing to destroy: nothing here stops a
    // supervised app or deletes its package, and a page is only ever a panel.
    case "app":
    case "url":
      return;
  }
  m.redraw();
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
  recordMembership(panelId);
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

// `awaitingInitialChat` flips on when the initial mount runs against an empty
// agent list -- a machine whose bootstrap is still creating its first chat --
// and back off as soon as that tab opens. While true, an agents_updated
// listener keeps retrying, and a launcher stands in until it lands.
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
  recordMembership(panelId);
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
 *  the same session from a sidebar row, a launcher row, or a layout restore
 *  focuses the existing tab instead of stacking a duplicate. */
function terminalPanelId(sessionName: string): string {
  return `${TERMINAL_PANEL_ID_PREFIX}${sessionName}`;
}

/** Rebuild a panel's params from its (deterministic) panel id.
 *
 *  Launcher, chat and persistent-terminal panel ids encode their identity, so a panel
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
  if (panelId.startsWith(LAUNCHER_PANEL_ID_PREFIX)) {
    return { panelType: "launcher", agentId: getPrimaryAgentId() };
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
  element.textContent = "This tab's contents could not be restored. Close it and open it again from the sidebar.";
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

/** A fresh id under ``prefix``, unique for as long as this workspace runs. */
function mintId(prefix: string): string {
  const unique = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Date.now());
  return `${prefix}${unique}`;
}

/** Mint a fresh per-tab terminal id. The backend maps this back to the tab's
 *  tmux client (via the pty) for live title tracking. */
function mintTerminalId(): string {
  return mintId("term-");
}

/** The primary agent's work_dir, or "" (the ttyd dispatch treats an empty
 *  work_dir as "start in $HOME"). New terminals anchor here. */
function primaryWorkDir(): string {
  return getAgentById(getPrimaryAgentId())?.work_dir ?? "";
}

// Cached snapshot of the live terminal-session fleet, re-read alongside the
// browser fleet (see ``refreshMachineInventory``). It is what lists a
// closed-but-alive terminal in the sidebar and the launcher, so it can be
// reattached rather than lost.
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

/** Open (or focus, if already open) a tab attached to ``sessionName``. Shared
 *  by "New terminal" (freshly allocated name) and the reattach path a sidebar
 *  row or a launcher row takes (existing name). ``options.panelId`` is used by
 *  the agent-driven ``service:terminal`` path so the server-minted panel id
 *  (and thus its ``terminal:<hash>`` ref) is preserved. */
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
  recordMembership(panelId);
  // The fleet gained a session the sidebar lists.
  refreshMachineInventory();
  return panelId;
}

/** "New terminal": allocate the next free ``terminal-N`` name from the backend,
 *  then open a tab attached to it. Returns the new panel's id, or null when the
 *  allocation failed. */
async function openNewTerminal(targetGroup?: DockviewGroupPanel | null): Promise<string | null> {
  if (!dockview) return null;
  let sessionName: string;
  try {
    sessionName = await allocateTerminalName();
  } catch (e) {
    // Allocation failed (backend unreachable); surface it rather than leaving
    // the "New terminal" click with no visible effect (matches the alert used
    // by the other terminal/agent actions in this file).
    alert(`Failed to open terminal: ${(e as Error).message}`);
    return null;
  }
  return addTerminalPanel(sessionName, { targetGroup });
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
        // Filed only once the session name has landed: the member ref is
        // ``terminal:<session>``, which the placeholder the panel was created
        // with cannot spell yet.
        recordMembership(panelId);
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
    recordMembership(panelId);
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
    recordMembership(panelId);
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
    recordMembership(panelId);
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
    recordMembership(panelId);
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
  recordMembership(panelId);
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
  recordMembership(panelId);
}

/**
 * Start a new object of ``tabType`` in ``targetGroup``.
 *
 * The multiplicity rules are the machine's (§7) and are not configurable here:
 * a chat always creates a new agent, a browser and a terminal always create new
 * instances, and a custom app focuses its single one. A chat and a browser open
 * their naming modal first -- neither the rail nor the launcher is a place to
 * type a name -- so the launcher that asked is only retired once the modal
 * reports back, which is why it is parked in ``newTabSourceLauncherPanelId``
 * rather than closed here.
 */
function openTabOfTypeInGroup(
  tabType: QuickAddTabType,
  targetGroup: DockviewGroupPanel | null,
  launcherPanelId: string | null,
): void {
  newTabTargetGroup = targetGroup;
  newTabSourceLauncherPanelId = launcherPanelId;
  if (tabType === "chat") {
    showNewChatModal = true;
    m.redraw();
    return;
  }
  if (tabType === "terminal") {
    void openNewTerminal(targetGroup).then((panelId) => {
      if (panelId !== null) retireLauncher(launcherPanelId);
      newTabSourceLauncherPanelId = null;
      m.redraw();
    });
    return;
  }
  if (tabType === "browser") {
    // Clean open: drop any leftover failure pre-fill so the modal fetches a
    // fresh random name and shows no error.
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
  if (backingApp === undefined) return;
  openAppTab(backingApp);
  retireLauncher(launcherPanelId);
  newTabSourceLauncherPanelId = null;
}

/** Open a new tab of ``tabType`` in the active pane. Exported for the sidebar's
 *  shortcut rows, which offer the machine's starting points as one-click
 *  rows. */
export function openTabOfType(tabType: QuickAddTabType): void {
  openTabOfTypeInGroup(tabType, null, null);
}

/** Open ``app``'s pane in the active project, focusing the one already open
 *  rather than stacking a second. Exported for the machine rail and the
 *  all-apps picker, and used by the launcher's app rows. */
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

// ---------- Membership ----------

// The member ref each live panel stands for, filled in as panels are created
// and as a restored layout is mounted, and dropped when a panel is disposed.
// Two things need it: filing a freshly-opened object into the active project,
// and answering "does this member have a tab right now" for the sidebar. It is
// a cache rather than a derivation because a ``url:<hash>`` ref is a SHA-256 of
// the panel id, which the platform only computes asynchronously.
const memberRefByPanelId = new Map<string, string>();

/** The part of a member ref after its scheme: a chat's agent id, a terminal's
 *  tmux session name, a service's name (plus the browser fleet's
 *  ``?session=<name>`` suffix). Every ref the store defines has the
 *  ``<scheme>:<body>`` shape -- see ``memberRef``, which builds them. */
function memberRefBody(ref: string): string {
  const separator = ref.indexOf(":");
  return separator === -1 ? ref : ref.substring(separator + 1);
}

/** The fleet browser a URL addresses (its ``?session=<name>``), or null when
 *  the URL names none or cannot be parsed. Each fleet browser is a separately
 *  addressable pane, so this is what keeps two of them from collapsing onto one
 *  ``service:browser`` member. */
function browserSessionFromUrl(url: string | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url, location.origin).searchParams.get("session");
  } catch {
    return null;
  }
}

/**
 * The member ref a panel is filed under, or null when it is not a member at all.
 *
 * Follows the grammar the store and ``layout_ops`` share (see
 * ``_panel_member_ref`` in projects.py), including its one deliberate
 * difference: a chat is filed under its stable agent id rather than the agent's
 * renameable display name, so membership survives a rename. A panel that names
 * nothing recognizable falls back to ``url:<hash>``, the form an ad-hoc URL
 * panel is addressed by. A launcher tab is not an object on the machine -- it
 * is a question about this pane -- so it is filed nowhere.
 */
async function memberRefForPanel(panelId: string): Promise<string | null> {
  const params = panelParams.get(panelId);
  if (params === undefined || params.panelType === "launcher") return null;
  if (params.panelType === "chat") {
    const chatAgentId = params.chatAgentId ?? params.agentId;
    return chatAgentId ? memberRef("chat", chatAgentId) : null;
  }
  if (params.terminalSessionName) return memberRef("terminal", params.terminalSessionName);
  if (params.serviceName) {
    const browserSession = params.serviceName === BROWSER_SERVICE_NAME ? browserSessionFromUrl(params.url) : null;
    return browserSession === null ? memberRef("app", params.serviceName) : memberRef("browser", browserSession);
  }
  return memberRef("url", await shortHash(panelId));
}

/** Record (and return) the ref a panel is filed under, so later lookups are
 *  synchronous. A panel that is not a member records nothing. */
async function rememberMemberRef(panelId: string): Promise<string | null> {
  const ref = await memberRefForPanel(panelId);
  if (ref === null) {
    memberRefByPanelId.delete(panelId);
    return null;
  }
  memberRefByPanelId.set(panelId, ref);
  return ref;
}

/**
 * Re-derive the refs of every live panel, and file any the view is not already
 * showing.
 *
 * Run after mounting a layout, where the panels arrive all at once rather than
 * through the creation paths that would have filed them. A panel always
 * corresponds to a member, so an arrangement that restores something the member
 * list lost -- a layout written before members existed, a hand-edited registry
 * -- files it again rather than leaving a docked tab the sidebar never lists.
 * Filing is idempotent, so the ordinary case is a run of no-ops.
 */
function reconcileMembersWithPanels(): void {
  void (async () => {
    memberRefByPanelId.clear();
    for (const panelId of panelParams.keys()) {
      await rememberMemberRef(panelId);
    }
    m.redraw();
    for (const panelId of panelParams.keys()) {
      recordMembership(panelId);
    }
  })();
}

/** The panel currently showing ``ref``, or null when the object is
 *  backgrounded (running, just not docked) or gone. */
function panelIdForMemberRef(ref: string): string | null {
  for (const [panelId, candidate] of memberRefByPanelId) {
    if (candidate === ref) return panelId;
  }
  return null;
}

/**
 * File a freshly-opened object into the view it was opened in.
 *
 * Opening is the only thing that adds a member: closing a tab deliberately
 * leaves the member list alone, so an object opened here stays listed
 * (backgrounded) until it is explicitly removed or destroyed. Adding is
 * idempotent and indifferent to what else shows the object -- membership is
 * many-to-many -- and Everything takes no members at all, since it shows
 * whatever the machine holds.
 *
 * Best-effort by construction: it runs after the tab is already open and
 * swallows every failure, because failing to reach the server must never stop a
 * tab from opening.
 */
function recordMembership(panelId: string): void {
  void (async () => {
    const ref = await rememberMemberRef(panelId);
    if (ref === null) return;
    const viewId = mountedViewId;
    // Nothing mounted means no project registry was reachable at startup, so
    // there is nowhere to file this either.
    if (viewId === null || isEverythingView(viewId)) return;
    if (projectForViewId(availableProjects, viewId)?.members.includes(ref) === true) return;
    try {
      await addMember(viewId, ref);
      await refreshProjectsList();
    } catch {
      // The object is open regardless, and opening it again files it again.
    }
  })();
}

/**
 * Take a destroyed object out of every project, and out of the dock if it is
 * still on screen.
 *
 * Destroy is the one cross-project operation. Closing a tab leaves both the
 * layout entry and the membership alone, but destroying tears down the agent,
 * terminal, or browser behind it, so it has to leave the projects that are not
 * currently mounted as well -- as a panel in their saved content, which would
 * otherwise restore a tab whose identity can no longer be resolved, and as a
 * member, which would otherwise keep listing it as backgrounded forever.
 *
 * ``panelId`` is the panel to sweep: the live one when the object had a tab,
 * else the deterministic id a chat or a named terminal is always filed under so
 * a backgrounded object is swept too. The server-side sweep is best-effort --
 * the destroy has already happened, and a project that still holds the panel
 * drops it the next time it is saved.
 */
async function forgetDestroyedObject(ref: string, panelId: string): Promise<void> {
  if (dockview) {
    const panel = dockview.panels.find((p) => p.id === panelId);
    if (panel) dockview.removePanel(panel);
  }
  try {
    await removePanelFromAllProjects(panelId, ref);
    await refreshProjectsList();
  } catch {
    // Best-effort; see above.
  }
}

async function saveLayout(): Promise<void> {
  if (!dockview) return;
  const targetViewId = mountedViewId;
  if (targetViewId === null) return;
  if (Date.now() < suppressAutosaveUntilMs) return;
  const payload = buildLayoutPayload();
  if (payload === null) return;
  const serialized = JSON.stringify(payload);
  // Content guard: an unchanged layout is neither re-persisted nor
  // re-broadcast, so remote re-applies cannot echo back and forth.
  if (serialized === lastPersistedLayoutJson) return;

  try {
    await autosaveProject(targetViewId, payload, getClientId());
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

/** Mark ``content`` as what the server currently holds for the active view,
 *  so the content guard in saveLayout can skip no-op persists. */
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

/**
 * Mount ``saved`` into the dockview, replacing whatever is currently shown.
 *
 * ``null`` -- a view with no saved content, or none that could be fetched --
 * mounts the New Tab launcher, which is what a freshly-created project opens
 * on. ``isInitialMount`` is the one exception: a machine whose starter project
 * has never been saved is a machine that has just booted, and the chat its
 * bootstrap created is what the user came for, so that one opens instead (and
 * ``awaitingInitialChat`` waits for it when the bootstrap is still running).
 * Switching to an empty project never takes that branch -- it is a project the
 * user just made, and the launcher is exactly the "pick what to start with"
 * surface the design asks for there.
 */
function applyLayoutContent(saved: SavedLayout | null, isInitialMount: boolean = false): void {
  if (!dockview) return;
  const dv = dockview;
  awaitingInitialChat = false;
  // Teardown removes every panel one by one; the dock-never-empty rule must not
  // fire a launcher into the middle of that.
  isApplyingLayout = true;

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

  // Whether the mount left the dock with nothing in it: a view saved with no
  // panels, a view that has never been saved, or one whose saved panels were
  // all services-agent chats we just stripped above.
  const isDockEmpty = dv.panels.length === 0;
  if (isDockEmpty && isInitialMount && saved === null) {
    // A freshly-booted machine: show the chat its bootstrap created, and keep
    // waiting when that agent has not been registered yet.
    if (!openInitialChatTab()) {
      awaitingInitialChat = true;
      openLauncherPanel(null);
    }
  } else if (isDockEmpty) {
    openLauncherPanel(null);
  }
  isApplyingLayout = false;
  // The restored panels arrived all at once rather than through the creation
  // paths, so their refs are derived (and filed) here instead.
  reconcileMembersWithPanels();
  scheduleTabWidthRecompute();
}

/** Record ``viewId`` as this browser's active view.
 *
 *  The active view IS this client's dockview state, so the id is mirrored onto
 *  the client-identity layout slug as well: that is the value
 *  ``reportClientState`` registers with the server and the one a sent chat
 *  message is attributed to, and leaving it unset would deregister the client
 *  entirely. */
function setActiveView(viewId: string): void {
  setActiveProjectId(viewId);
  setActiveLayoutSlug(viewId);
  mountedViewId = viewId;
}

/**
 * Pick this client's initial view (its stored per-browser choice, else the
 * first project), register it with the server, and mount its content. Runs once
 * at startup, after the dockview exists.
 */
async function initializeActiveView(): Promise<void> {
  const listResponse = await fetchProjectsList();
  availableProjects = listResponse.projects;
  const chosenId = chooseInitialViewId(availableProjects, getStoredProjectId());
  if (chosenId === null) {
    // No projects at all (server unreachable / no primary agent): run with the
    // fresh-workspace state; nothing persists.
    applyLayoutContent(null, true);
    m.redraw();
    return;
  }
  setActiveView(chosenId);
  reportClientState();
  refreshMachineInventory();
  const saved = (await fetchProjectContent(chosenId)) as SavedLayout | null;
  markServerContent(saved);
  applyLayoutContent(saved, true);
  m.redraw();
}

/**
 * Switch this client onto another view: flush pending edits into the old one,
 * repoint the autosave target, tell the server (which records the switch
 * event), and mount the new view's content.
 *
 * Everything is switched to exactly like a project -- it has a layout of its
 * own, so there is no lens mode and no dock to leave in place. Exported for the
 * sidebar's switcher, which owns the *choice* and hands the load over here.
 */
export async function switchToView(viewId: string): Promise<void> {
  if (!dockview) return;
  const previousViewId = mountedViewId ?? getActiveProjectId();
  if (previousViewId === viewId) return;
  await flushPendingSave();
  setActiveView(viewId);
  reportClientState(previousViewId);
  refreshMachineInventory();
  const saved = (await fetchProjectContent(viewId)) as SavedLayout | null;
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
    if (event.projectId === mountedViewId && event.savedByClientId !== getClientId()) {
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
    if (event.projectId === mountedViewId) {
      void switchToView(event.fallbackId).then(() => {
        alert(`Project "${deletedName}" was deleted; switched to "${displayNameForProject(event.fallbackId)}".`);
      });
    }
    return;
  }
  // Renamed / restyled, or a member list moved (here or in another project):
  // the content is untouched either way, so re-list and let the sidebar
  // repaint from the fresh registry.
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

  // A pane resize changes a strip's width without changing the layout, so the
  // strips are watched directly; ``recomputeTabWidths`` re-observes them as
  // groups come and go.
  tabStripObserver = new ResizeObserver(() => {
    scheduleTabWidthRecompute();
  });
  window.addEventListener("resize", scheduleTabWidthRecompute);

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

        case "launcher":
          return createLauncherRenderer(options.id);

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
    // The "+" sits after each pane's tabs (§5), so it is a right-hand header
    // action rather than a left one.
    createRightHeaderActionComponent(group) {
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
    // ...and it changes how many tabs each strip is fitting.
    scheduleTabWidthRecompute();
  });

  // Clean up the bookkeeping a disposed panel leaves behind. Closing a tab is
  // deliberately nothing more than that: the object it was showing keeps
  // running, stays a member of every project holding it, and stays listed in
  // the sidebar as backgrounded. What the close must not leave behind is an
  // empty dock, so an emptied view falls back to the launcher.
  dv.api.onDidRemovePanel((panel) => {
    panelParams.delete(panel.id);
    memberRefByPanelId.delete(panel.id);
    ensureDockIsNotEmpty();
    scheduleTabWidthRecompute();
  });
  dv.api.onDidAddPanel(() => {
    scheduleTabWidthRecompute();
  });

  // While awaitingInitialChat is true, every agents_updated event is
  // another chance for the bootstrap-created chat agent to show up.
  agentsUpdatedListener = () => {
    if (!awaitingInitialChat) return;
    const launcherPanelId = dv.panels.find((panel) => panelParams.get(panel.id)?.panelType === "launcher")?.id ?? null;
    if (openInitialChatTab()) {
      awaitingInitialChat = false;
      // The launcher was only standing in until the chat arrived.
      retireLauncher(launcherPanelId);
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

  // Pick this browser's active view and mount its content.
  void initializeActiveView();
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

  await forgetDestroyedObject(memberRef("chat", agentId), panelId);

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

  await forgetDestroyedObject(memberRef("terminal", sessionName), panelId);

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

  await forgetDestroyedObject(memberRef("browser", name), panelId);

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
                retireLauncher(newTabSourceLauncherPanelId);
                newTabSourceLauncherPanelId = null;
              },
              onCancel() {
                showNewChatModal = false;
                newTabTargetGroup = null;
                newTabSourceLauncherPanelId = null;
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
                retireLauncher(newTabSourceLauncherPanelId);
                newTabSourceLauncherPanelId = null;
              },
              onCancel() {
                showNewAgentModal = false;
                newTabTargetGroup = null;
                newTabSourceLauncherPanelId = null;
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
                retireLauncher(newTabSourceLauncherPanelId);
                newTabSourceLauncherPanelId = null;
                // The accept succeeded optimistically; clear any leftover failure
                // pre-fill so a subsequent clean open starts fresh.
                newBrowserPrefillName = null;
                newBrowserError = null;
                return createdPane;
              },
              // The background create POST succeeded: the modal is already closed
              // and the pane already open, so just refresh the fleet so the
              // sidebar and the launcher list the new browser.
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
                newTabSourceLauncherPanelId = null;
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
