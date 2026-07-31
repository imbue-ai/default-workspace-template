/**
 * The serialized shape of a saved dockview layout, and the one definition of
 * when two of them mean the same thing.
 *
 * All desktop-class clients share the `desktop` layout: an autosave broadcasts
 * `layout_saved` and every other client re-applies it. Raw JSON equality cannot
 * settle that loop, because a layout's serialization is partly client-specific
 * -- pixel sizes come from the window, the active tab is per-user, terminal ids
 * are minted per tab -- so client A's save never matches client B's
 * serialization of the very same layout. Each side then sees a difference,
 * saves, and provokes the other: two clients traded saves every 1-2 seconds.
 *
 * The projection below drops exactly those client-specific fields, so
 * "semantically the same layout" becomes decidable. Both users of that answer
 * share this function deliberately: the autosave guard (don't persist a layout
 * that is semantically what the server already holds) and the remote-apply skip
 * (don't rebuild the dockview for a layout that means what is already on
 * screen). If the two ever disagreed, one of them would resurrect the loop.
 */

import type { SerializedDockview } from "dockview-core";

export type PanelType = "chat" | "iframe" | "subagent";

export interface PanelParams {
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

export interface SavedLayout {
  dockview: SerializedDockview;
  panelParams: Record<string, PanelParams>;
}

/**
 * Keys dropped wherever they appear in a serialized layout.
 *
 * `size` / `width` / `height` / `initialWidth` / `initialHeight` are pixel
 * geometry, which is a property of the window a layout is being shown in rather
 * than of the layout. `activeView` / `activeGroup` are which tab each user is
 * looking at, which is personal to that user and must never be synced.
 */
const CLIENT_SPECIFIC_KEYS: ReadonlySet<string> = new Set([
  "size",
  "width",
  "height",
  "initialWidth",
  "initialHeight",
  "activeView",
  "activeGroup",
]);

/** Panel-params keys dropped for a terminal tab. `terminalId` is minted per tab
 *  and the ttyd `url` embeds it, so both differ between clients showing the very
 *  same terminal; `terminalSessionId` is tmux's own handle, learned live. The
 *  tmux session name is the tab's real identity and is kept. */
const TERMINAL_ONLY_KEYS: readonly string[] = ["terminalId", "terminalSessionId", "url"];

function isTerminalPanel(params: Record<string, unknown>): boolean {
  return params.terminalSessionName !== undefined || params.terminalId !== undefined;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Strip a panel-params object down to its semantic content. */
function projectPanelParams(params: Record<string, unknown>): Record<string, unknown> {
  if (!isTerminalPanel(params)) {
    return params;
  }
  const projected: Record<string, unknown> = { ...params };
  for (const key of TERMINAL_ONLY_KEYS) {
    delete projected[key];
  }
  return projected;
}

/**
 * Recursively drop client-specific fields and sort object keys.
 *
 * Sorting matters as much as dropping: `panelParams` and dockview's `panels` are
 * keyed maps built by iterating a `Map`, so two clients holding identical
 * layouts serialize them in whatever order each happened to insert panels --
 * a difference of pure spelling that JSON equality would report as a change.
 * `undefined` values are dropped too, so an absent key and an explicitly
 * undefined one compare equal.
 */
function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(canonicalize);
  }
  if (!isPlainObject(value)) {
    return value;
  }
  const source = "panelType" in value ? projectPanelParams(value) : value;
  const result: Record<string, unknown> = {};
  for (const key of Object.keys(source).sort()) {
    if (CLIENT_SPECIFIC_KEYS.has(key) || source[key] === undefined) {
      continue;
    }
    result[key] = canonicalize(source[key]);
  }
  return result;
}

/** A stable string identifying a layout's semantic content, for comparison and
 *  for remembering what the server holds. `null` (a layout with no content) has
 *  its own key, distinct from an empty layout. */
export function layoutContentKey(saved: SavedLayout | null): string {
  return saved === null ? "" : JSON.stringify(canonicalize(saved));
}

/** Whether two layouts describe the same arrangement, ignoring the fields that
 *  legitimately differ between two clients showing it. */
export function layoutContentsAreEquivalent(a: SavedLayout | null, b: SavedLayout | null): boolean {
  return layoutContentKey(a) === layoutContentKey(b);
}
