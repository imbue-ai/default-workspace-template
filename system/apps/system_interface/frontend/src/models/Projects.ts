/**
 * API client + pure helpers for projects.
 *
 * A project is a **view** over the machine's objects: a filter saying which of
 * them it shows (`service:<name>`, `service:browser?session=<name>`,
 * `chat:<agent-id>`, `terminal:<name>`, `url:<hash>`), plus its own saved
 * dockview arrangement. Membership is explicit -- it changes only through the
 * member calls below, never as a side effect of opening or closing a tab -- so
 * a member with no panel is simply backgrounded: still running, just not
 * docked.
 *
 * Membership is many-to-many and nothing owns anything: the same object may
 * appear in any number of projects at once, so there is no owner and no
 * "move". `removeMember` hides an object in one view and nowhere else.
 *
 * "Everything" is the view with no filter, and it is the *home*: every object
 * on the machine appears in it, including objects filed in no project at all.
 * It keeps its own layout like any other view -- `fetchProjectContent` and
 * `autosaveProject` take EVERYTHING_VIEW_ID -- but it is not a project: it
 * never comes back from `fetchProjectsList`, has no member list, and cannot be
 * renamed or deleted. Its tab list is built by enumerating the machine (see
 * buildEverythingMembers), not by unioning member lists, because objects filed
 * nowhere must still show up. See DockviewWorkspace and Sidebar for the
 * consuming logic.
 */

import { apiUrl } from "../base-path";
import { getDeviceKind } from "./ClientIdentity";

/**
 * The id of the reserved unfiltered view, matching the backend's
 * ``EVERYTHING_VIEW_ID``.
 *
 * It is a view id but not a project id: the server stores its layout under
 * this id and rejects nothing when it is used for content, yet it has no
 * registry entry, so it never appears in the project list and must never be
 * posted to a member, settings, or delete endpoint.
 */
export const EVERYTHING_VIEW_ID = "everything";

/** The display name of the unfiltered view, matching the backend's
 *  ``EVERYTHING_VIEW_NAME``. */
export const EVERYTHING_VIEW_NAME = "Everything";

/** Whether a view id addresses the unfiltered view rather than a project. The
 *  views branch on this constantly: Everything has a layout but no members,
 *  no settings and no delete. */
export function isEverythingView(viewId: string): boolean {
  return viewId === EVERYTHING_VIEW_ID;
}

/** One shortcut's stored deviations from the code-side defaults. Sparse on the
 *  wire: an absent entry (or field) means the default, and the server may spell
 *  an unset field as null. */
export interface ShortcutOverride {
  is_pinned?: boolean | null;
  mode?: string | null;
}

export interface ProjectInfo {
  project_id: string;
  name: string;
  color: string;
  glyph: number;
  has_content: boolean;
  // Every ref this project shows, open or backgrounded, in the order they were
  // added. Not derived from the layout: a member with no panel is still here.
  // The same ref may appear in other projects' lists too.
  members: string[];
  // Per-shortcut deviations from the defaults, keyed by a built-in name or
  // ``app:<service-name>``: ``is_pinned: false`` moves a built-in row into the
  // All apps menu (app pinning IS membership, so app: keys never carry it),
  // and ``mode`` flips what clicking the row does. Sparse so a project that
  // has never touched this -- which is every project until it does -- keeps
  // every default; optional for the same reason on a server predating it.
  shortcut_overrides?: Record<string, ShortcutOverride>;
}

/** The rail's built-in shortcut rows, in the order the rail offers them.
 *  Unlike an app, none of these is an object with a member ref, so which of
 *  them a project shows is its own stored field rather than membership. */
export const SHORTCUT_NAMES = ["chat", "files", "browser", "terminal"] as const;

export type ShortcutName = (typeof SHORTCUT_NAMES)[number];

/** A shortcut's two modes: focus goes to the most recently used member of the
 *  kind in the active view (creating only when it shows none), new always
 *  creates. */
export type ShortcutMode = "focus" | "new";

/** The shortcut-override key for one pinned app, mirroring the server's
 *  ``app:<service-name>`` grammar. */
export function appShortcutId(serviceName: string): string {
  return `app:${serviceName}`;
}

/** The code-side mode default per shortcut: chat starts in new mode ("New
 *  Chat" -- multi-chat discoverability is the point), everything else --
 *  files, browser, terminal, and every app -- in focus mode. Matching the
 *  server's ``default_shortcut_mode``, since only deviations are stored. */
export function defaultShortcutMode(shortcutId: string): ShortcutMode {
  return shortcutId === "chat" ? "new" : "focus";
}

/** Whether this project keeps ``shortcut`` in its rail. Absent means all four,
 *  so a project the server told us nothing about shows the full set. */
export function isShortcutPinned(project: ProjectInfo | null, shortcut: ShortcutName): boolean {
  return project?.shortcut_overrides?.[shortcut]?.is_pinned !== false;
}

/** The effective mode of one shortcut in one project: the stored override when
 *  there is a valid one, else the code-side default. Everything (a null
 *  project) has no entry to store against, so it is always the defaults. */
export function shortcutModeForProject(project: ProjectInfo | null, shortcutId: string): ShortcutMode {
  const mode = project?.shortcut_overrides?.[shortcutId]?.mode;
  if (mode === "focus" || mode === "new") return mode;
  return defaultShortcutMode(shortcutId);
}

export interface ProjectsListResponse {
  projects: ProjectInfo[];
  last_active_id: string | null;
}

/** Fetch the project registry. Everything is never in it -- it has no registry
 *  entry -- so `last_active_id` is the one field that may name it. Defensive:
 *  an unreachable server yields an empty list so the workspace still renders
 *  (nothing will persist). */
export async function fetchProjectsList(): Promise<ProjectsListResponse> {
  try {
    const response = await fetch(apiUrl("/api/projects"));
    if (!response.ok) return { projects: [], last_active_id: null };
    const data = (await response.json()) as { projects?: ProjectInfo[]; last_active_id?: string | null };
    return { projects: data.projects ?? [], last_active_id: data.last_active_id ?? null };
  } catch {
    return { projects: [], last_active_id: null };
  }
}

/** Fetch one view's saved content, EVERYTHING_VIEW_ID included -- the
 *  unfiltered view has its own layout like any project. Reads this client's
 *  own device kind's arrangement (a view is arranged per device; membership is
 *  shared). Returns null both for a view that has never been saved on this
 *  device (render the New Tab launcher) and on any fetch failure. */
export async function fetchProjectContent(viewId: string): Promise<unknown | null> {
  try {
    const response = await fetch(
      apiUrl(`/api/projects/${encodeURIComponent(viewId)}?device=${encodeURIComponent(getDeviceKind())}`),
    );
    if (!response.ok) return null;
    const data = (await response.json()) as { layout?: unknown };
    return data.layout ?? null;
  } catch {
    return null;
  }
}

// What the tunnel in front of this server answers when the server itself is
// not up: a workspace still provisioning, restarting, or shutting down. The
// request never reached an endpoint, so nothing was read and nothing changed.
const UNREACHABLE_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);

/**
 * Why a request failed, in terms the user can act on.
 *
 * The server's own ``detail`` when it gave one -- it knows best. Otherwise the
 * bare status was being shown, and "HTTP 503" reads as a bug in the thing you
 * just clicked when it actually means the workspace was not answering at all.
 * Saying so, and saying that nothing changed, is the difference between "this
 * feature is broken" and "try again in a moment".
 */
async function errorDetailFromResponse(response: Response): Promise<string> {
  const data = (await response.json().catch(() => ({}))) as { detail?: string };
  if (data.detail) return data.detail;
  if (UNREACHABLE_STATUSES.has(response.status)) {
    return "the workspace is not responding right now, so nothing was changed — try again in a moment";
  }
  return `HTTP ${response.status}`;
}

/** Autosave the active view's content, EVERYTHING_VIEW_ID included, into this
 *  client's own device kind's arrangement. Throws on failure (callers treat
 *  autosave as best-effort and catch). */
export async function autosaveProject(viewId: string, layoutPayload: unknown, clientId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(viewId)}`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ layout: layoutPayload, client_id: clientId, device: getDeviceKind() }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
}

/** Create an empty project with the given display metadata. Throws with the
 *  server's detail on rejection (bad name, id conflict). */
export async function createProject(name: string, color: string, glyph: number): Promise<ProjectInfo> {
  const response = await fetch(apiUrl("/api/projects"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, color, glyph }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  return (await response.json()) as ProjectInfo;
}

/** Rename/restyle an existing project. The id is stable across edits, so the
 *  project's saved content and member list are untouched. Throws with the
 *  server's detail on rejection (unknown project, bad name, bad color or
 *  glyph). */
export async function updateProjectSettings(
  projectId: string,
  name: string,
  color: string,
  glyph: number,
): Promise<ProjectInfo> {
  const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/settings`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, color, glyph }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  return (await response.json()) as ProjectInfo;
}

/**
 * Record one shortcut's pin or mode override on this project -- the one write
 * path the UI and the agent-facing `layout.py shortcut set` share.
 * Project-scoped: which starting points a project keeps to hand, and what
 * clicking each does, belong to that project, not to the user or the device.
 *
 * At least one of `isPinned` / `mode` must be given. Returns the project's
 * resulting full override map. Throws with the server's detail on rejection
 * (an unknown project, a bad shortcut id or mode, a pin on an app: key).
 */
export async function setShortcutOverride(
  projectId: string,
  shortcutId: string,
  override: { isPinned?: boolean; mode?: ShortcutMode },
): Promise<Record<string, ShortcutOverride>> {
  const body: Record<string, unknown> = { shortcut: shortcutId };
  if (override.isPinned !== undefined) body.is_pinned = override.isPinned;
  if (override.mode !== undefined) body.mode = override.mode;
  const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/shortcuts`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  const data = (await response.json()) as { shortcut_overrides?: Record<string, ShortcutOverride> };
  return data.shortcut_overrides ?? {};
}

/** Delete a project. This is a pure view operation: only the project's
 *  registry entry, member list and saved content go, and the server never
 *  touches the objects it showed -- they keep running, and stay in Everything
 *  and in any other project already showing them. A machine may end up with
 *  zero projects; Everything is always there. Throws with the server's detail
 *  on rejection (unknown project). */
export async function deleteProjectRequest(projectId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/delete`), { method: "POST" });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
}

/**
 * Drop one destroyed panel -- and, when given, the member it stood for --
 * from every project, returning the ids that changed.
 *
 * This is the storage half of destroying a tab, and the one path that reaches
 * across projects. Closing a tab leaves both the layout entry and the
 * membership alone, but destroying tears down the agent, terminal, or browser
 * behind it, so it has to leave the projects that are not currently mounted as
 * well -- otherwise switching to one of them would restore a tab whose
 * identity can no longer be resolved, and the sidebar would list the object as
 * backgrounded forever. Destroying is also the only thing that takes an object
 * out of Everything, since Everything lists whatever the machine still holds.
 * Callers that know only the panel omit `ref` and drop the panel alone.
 * Best-effort: a failure here must not block the destroy itself, so callers
 * catch.
 */
export async function removePanelFromAllProjects(panelId: string, ref?: string): Promise<string[]> {
  const response = await fetch(apiUrl(`/api/projects/panels/${encodeURIComponent(panelId)}/delete`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref: ref ?? null }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  const data = (await response.json()) as { project_ids?: string[] };
  return data.project_ids ?? [];
}

/** Show `ref` in a project. Idempotent, and indifferent to what else shows it:
 *  a project is a view, so the same object appearing in several at once is
 *  ordinary rather than a conflict. Throws with the server's detail on
 *  rejection (unknown project). */
export async function addMember(projectId: string, ref: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/members`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
}

/** Stop showing `ref` in one project, leaving the object itself running. This
 *  is "remove from project": it keeps running, it stays in every other project
 *  showing it, and it stays in Everything. Throws with the server's detail on
 *  rejection (unknown project). */
export async function removeMember(projectId: string, ref: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/members/remove`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
}

/**
 * Also show `ref` in another project, returning every project showing it
 * afterwards.
 *
 * This is what opening something from the launcher's "on this machine" table
 * does: the object joins the project you are looking at and is taken from
 * nowhere. It differs from `addMember` only in addressing the destination in
 * the body rather than the path, which is what lets a caller that holds a ref
 * but no project context file it. Throws with the server's detail on rejection
 * (unknown destination).
 */
export async function shareMember(ref: string, toProjectId: string): Promise<string[]> {
  const response = await fetch(apiUrl("/api/projects/members/share"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref, to_project_id: toProjectId }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  const data = (await response.json()) as { projects?: string[] };
  return data.projects ?? [];
}

/** Fetch the machine-wide ref -> showing-projects map. A ref maps to every
 *  project whose filter includes it, and a ref no project holds is simply
 *  absent -- it still exists on the machine, and Everything lists it anyway.
 *  Defensive like fetchProjectsList: an unreachable server yields an empty
 *  map, which reads as "nothing is filed anywhere" rather than breaking the
 *  sidebar. */
export async function fetchMemberMap(): Promise<Record<string, string[]>> {
  try {
    const response = await fetch(apiUrl("/api/projects/members"));
    if (!response.ok) return {};
    const data = (await response.json()) as { members?: Record<string, string[]> };
    return data.members ?? {};
  } catch {
    return {};
  }
}

/**
 * Pick the view a client should mount on: its stored per-browser choice when
 * that view still exists, else the first project, else Everything.
 *
 * A machine may genuinely have zero projects now that deleting one is a pure
 * view operation with no undeletable project left, and a registry that could
 * not be read (server unreachable) looks the same as one holding none --
 * either way Everything is always there to land on, so there is always a view
 * to name and this never comes back empty-handed.
 *
 * A client last looking at Everything lands back on Everything: it is the home
 * and has a layout of its own, so there is nothing to fall back from.
 */
export function chooseInitialViewId(projects: readonly ProjectInfo[], storedId: string): string {
  if (isEverythingView(storedId)) return EVERYTHING_VIEW_ID;
  const stored = projects.find((project) => project.project_id === storedId);
  if (stored) return stored.project_id;
  return projects.length === 0 ? EVERYTHING_VIEW_ID : projects[0].project_id;
}

/**
 * Resolve a view id to the project backing it, or null when nothing does.
 *
 * Null covers both Everything, which is a view with no registry entry, and a
 * project that has been deleted since the id was recorded -- callers that need
 * a project's name, color, glyph or members have to handle both the same way.
 */
/** The project an agent-driven open should file its object into: the
 *  requesting agent's own project (its ``project`` label) when that project
 *  is actually registered, else null -- the caller falls back to the view the
 *  tab opened in. Keeps agent-opened tabs landing in the agent's project
 *  rather than whichever view the user happened to be looking at. */
export function filingProjectForAgentOp(
  requesterProject: string | null | undefined,
  projects: readonly ProjectInfo[],
): string | null {
  if (!requesterProject) return null;
  return projects.some((project) => project.project_id === requesterProject) ? requesterProject : null;
}

export function projectForViewId(projects: readonly ProjectInfo[], viewId: string): ProjectInfo | null {
  return projects.find((project) => project.project_id === viewId) ?? null;
}

/** The kinds of object a member ref addresses: what the sidebar picks an icon
 *  for, and what its search matches against so "browser" keeps browsers.
 *
 *  CLEANUP: drop "url" (and every branch that handles it) once the server's
 *  legacy purge (``_purge_legacy_members_unlocked`` in projects.py) has
 *  run on all supported workspaces -- ad-hoc pages are no longer filed as
 *  members, and the purge deletes the entries older registries still hold, so
 *  no client will ever see a "url" member again after that. */
export type MemberKind = "chat" | "browser" | "terminal" | "app" | "url";

const CHAT_REF_PREFIX = "chat:";
const TERMINAL_REF_PREFIX = "terminal:";
const URL_REF_PREFIX = "url:";
const SERVICE_REF_PREFIX = "service:";
const BROWSER_SERVICE_NAME = "browser";
const TERMINAL_SERVICE_NAME = "terminal";

/**
 * Classify one member ref.
 *
 * The grammar is the store's (see projects.py): `chat:<agent-id>`,
 * `terminal:<session>`, `url:<hash>` for an ad-hoc page, and `service:<name>`
 * with the browser fleet's `?session=<id>` suffix. The browser viewer and the
 * terminal are registered services like any other, but they are fleets rather
 * than installed apps and the sidebar lists them as their own kinds. Anything
 * unrecognized -- only reachable through a hand-edited registry -- falls back
 * to the generic app row.
 */
export function memberKindFromRef(ref: string): MemberKind {
  if (ref.startsWith(CHAT_REF_PREFIX)) return "chat";
  if (ref.startsWith(TERMINAL_REF_PREFIX)) return "terminal";
  if (ref.startsWith(URL_REF_PREFIX)) return "url";
  if (ref.startsWith(SERVICE_REF_PREFIX)) {
    const serviceName = ref.substring(SERVICE_REF_PREFIX.length).split("?")[0];
    if (serviceName === BROWSER_SERVICE_NAME) return "browser";
    if (serviceName === TERMINAL_SERVICE_NAME) return "terminal";
  }
  return "app";
}

/**
 * Build the ref one object of a given kind is filed under.
 *
 * The inverse of memberKindFromRef, and the one place the views form refs, so
 * the grammar the store and `layout_ops` share is written down once. `name` is
 * whatever identifies the object within its kind: a chat's stable agent id (not
 * its renameable display name), a tmux session name, a fleet browser's session
 * name, a service name, or the short hash an ad-hoc URL panel is addressed by
 * (which only the live layout can compute, hence taking it rather than
 * deriving it).
 */
export function memberRef(kind: MemberKind, name: string): string {
  switch (kind) {
    case "chat":
      return `${CHAT_REF_PREFIX}${name}`;
    case "terminal":
      return `${TERMINAL_REF_PREFIX}${name}`;
    case "url":
      return `${URL_REF_PREFIX}${name}`;
    case "browser":
      return `${SERVICE_REF_PREFIX}${BROWSER_SERVICE_NAME}?session=${name}`;
    case "app":
      return `${SERVICE_REF_PREFIX}${name}`;
  }
}

/** The stable agent id out of a `chat:<agent-id>` ref, or null for a ref that
 *  addresses no chat. The inverse of `memberRef("chat", id)`. */
export function chatAgentIdFromRef(ref: string): string | null {
  if (!ref.startsWith(CHAT_REF_PREFIX)) return null;
  const id = ref.substring(CHAT_REF_PREFIX.length);
  return id === "" ? null : id;
}

/**
 * The service name a `service:<name>` ref addresses, or null for a ref that
 * addresses no service.
 *
 * The partial inverse of `memberRef("app", name)`, kept beside it so the
 * grammar stays written down in one place: the views that need to look an app
 * up from a row (its icon, say) ask here rather than slicing the ref
 * themselves. An app INSTANCE ref (`service:<name>?instance=<name>-<N>`)
 * answers its service's name -- the instance is a page of that service, so
 * its icon, liveness and share surface are the service's. A fleet ref
 * (`service:browser?session=...`) names the fleet's service and not an
 * installed app, so it answers null -- the browser and the terminal are
 * their own kinds everywhere else too.
 */
export function serviceNameFromRef(ref: string): string | null {
  if (!ref.startsWith(SERVICE_REF_PREFIX)) return null;
  const body = ref.substring(SERVICE_REF_PREFIX.length);
  const queryIndex = body.indexOf("?");
  if (queryIndex === -1) {
    return body === "" ? null : body;
  }
  const name = body.substring(0, queryIndex);
  if (name === "" || instanceNameFromRef(ref) === null) return null;
  return name;
}

/** The query key an app instance's ref carries its canonical name under,
 *  mirroring the backend's `app_instances.INSTANCE_QUERY_KEY`. */
const INSTANCE_QUERY_KEY = "instance";

/** The member ref one app instance is filed under:
 *  `service:<service>?instance=<instance>` with the FULL canonical instance
 *  name (`files-2`) in the query, mirroring the backend's
 *  `app_instances.instance_ref`. Built without URL-encoding on purpose: the
 *  allocator is the only writer, and a canonical name is a registered service
 *  name (a DNS label) plus `-<N>`, which carries nothing the query-decoding
 *  parsers would transform. */
export function appInstanceRef(serviceName: string, instanceName: string): string {
  return `${SERVICE_REF_PREFIX}${serviceName}?${INSTANCE_QUERY_KEY}=${instanceName}`;
}

/** The canonical instance name out of an instance ref, or null for a ref that
 *  names no instance (a bare service ref -- an app's pin -- or the browser
 *  fleet's `?session=` form). */
export function instanceNameFromRef(ref: string): string | null {
  if (!ref.startsWith(SERVICE_REF_PREFIX)) return null;
  const body = ref.substring(SERVICE_REF_PREFIX.length);
  const queryIndex = body.indexOf("?");
  if (queryIndex === -1 || queryIndex === 0) return null;
  const instanceName = new URLSearchParams(body.substring(queryIndex + 1)).get(INSTANCE_QUERY_KEY);
  return instanceName === null || instanceName === "" ? null : instanceName;
}

/** `<service>-<N>`: the canonical instance name the allocator mints, always
 *  the full registered service name plus a dash and a 1-based number -- so
 *  the final `-<digits>` group always separates the two, even for a service
 *  whose own name ends in digits. Mirrors the backend's
 *  `app_instances._INSTANCE_NAME_PATTERN`. */
const INSTANCE_NAME_PATTERN = /^(.+)-([1-9][0-9]*)$/;

/** The 1-based number out of a canonical `<service>-<N>` instance name, or
 *  null when the name does not carry one (a hand-edited ref). */
export function instanceNumberFromName(instanceName: string): number | null {
  const match = INSTANCE_NAME_PATTERN.exec(instanceName);
  return match === null ? null : Number(match[2]);
}

/** The registered service name out of a canonical `<service>-<N>` instance
 *  name, or null when the name does not parse. The inverse of the allocator's
 *  mint, which always appends `-<N>` to the full service name. */
export function serviceNameFromInstanceName(instanceName: string): string | null {
  const match = INSTANCE_NAME_PATTERN.exec(instanceName);
  return match === null ? null : match[1];
}

/** One object as the machine reports it, before it becomes a row: the name its
 *  ref is built from (see memberRef) and what to call it in the UI. */
export interface MachineObject {
  name: string;
  label: string;
}

/** One app instance as the machine reports it: which service it is a page of,
 *  its canonical instance name (the ref is built from both -- see
 *  appInstanceRef), and what to call it in the UI. */
export interface AppInstanceObject {
  serviceName: string;
  instanceName: string;
  label: string;
}

/**
 * Everything the machine currently holds, gathered per kind from the source
 * that knows about it: chat agents from the agent list, terminals from the
 * tmux fleet, browsers from the browser fleet, and app INSTANCES from the
 * instance inventory (derived server-side from member lists and saved
 * layouts -- see models/AppInstances). A registered app with no instances is
 * openable (the rail, the popover, the launcher tiles all offer it) but is
 * not an object here: the tab lists hold instances, never bare services.
 *
 * Those four kinds are the whole of it, because they are the four the machine
 * can enumerate: an ad-hoc URL page exists only as a panel in some view's
 * arrangement (see `memberRefForPanelParams` in DockviewWorkspace, which files
 * none), so nothing here can report one and a `url:` ref only ever reaches a
 * tab list through a migrated project's own member list.
 */
export interface MachineInventory {
  chatAgents: readonly MachineObject[];
  terminals: readonly MachineObject[];
  browsers: readonly MachineObject[];
  appInstances: readonly AppInstanceObject[];
}

/** One row of a tab list: the object, what it is called, and which projects
 *  show it. `projectIds` is empty for an object filed in no project at all,
 *  which is ordinary -- Everything is its home. */
export interface MemberRow {
  ref: string;
  kind: MemberKind;
  label: string;
  projectIds: string[];
}

const INVENTORY_KINDS: readonly { kind: MemberKind; key: "chatAgents" | "terminals" | "browsers" }[] = [
  { kind: "chat", key: "chatAgents" },
  { kind: "terminal", key: "terminals" },
  { kind: "browser", key: "browsers" },
];

/**
 * Build Everything's tab list by enumerating the machine.
 *
 * Everything is the unfiltered view, so its rows come from what exists rather
 * than from the union of the projects' member lists: an object filed in no
 * project at all -- a side chat, a terminal nobody put anywhere -- has to
 * appear here, and unioning member lists would silently drop exactly those.
 * `projectsByRef` (see fetchMemberMap) only decorates each row with the
 * projects showing it, for the row menu; a ref missing from it is filed
 * nowhere, not hidden.
 *
 * Kinds come out in inventory order (chats, terminals, browsers, app
 * instances) and objects within a kind in the order their source listed them.
 * Duplicate refs collapse onto the first row for them, so an object a source
 * reports twice does not list twice.
 */
export function buildEverythingMembers(
  inventory: MachineInventory,
  projectsByRef: Readonly<Record<string, readonly string[]>>,
): MemberRow[] {
  const rows: MemberRow[] = [];
  const seenRefs = new Set<string>();
  const pushRow = (ref: string, kind: MemberKind, label: string): void => {
    if (seenRefs.has(ref)) return;
    seenRefs.add(ref);
    rows.push({ ref, kind, label, projectIds: [...(projectsByRef[ref] ?? [])] });
  };
  for (const { kind, key } of INVENTORY_KINDS) {
    for (const object of inventory[key]) {
      if (object.name === "") continue;
      pushRow(memberRef(kind, object.name), kind, object.label);
    }
  }
  // Apps list as their instances: an instance is the object the way a chat or
  // a terminal is, while a zero-instance app has no tab-list row anywhere --
  // the rail, the popover and the launcher tiles are where it stays openable.
  for (const instance of inventory.appInstances) {
    if (instance.serviceName === "" || instance.instanceName === "") continue;
    pushRow(appInstanceRef(instance.serviceName, instance.instanceName), "app", instance.label);
  }
  return rows;
}

/** A machine-wide list split by whether the project shows each object. The
 *  New Tab launcher's "On this machine" table is the onMachine half; its "In
 *  this project" table comes from the member list itself, not from here. */
export interface MembershipPartition<T> {
  inProject: T[];
  onMachine: T[];
}

/**
 * Split a machine-wide object list into the ones this project shows and the
 * rest.
 *
 * "The rest" is everything else on the machine, whether it is filed in other
 * projects or in none: membership is many-to-many, so opening one of them from
 * the launcher adds it here and takes it from nowhere. Input order is
 * preserved within each half.
 */
export function partitionByMembership<T extends { ref: string }>(
  objects: readonly T[],
  memberRefs: readonly string[],
): MembershipPartition<T> {
  const members = new Set(memberRefs);
  const partition: MembershipPartition<T> = { inProject: [], onMachine: [] };
  for (const object of objects) {
    if (members.has(object.ref)) {
      partition.inProject.push(object);
    } else {
      partition.onMachine.push(object);
    }
  }
  return partition;
}

/** A `[start, end)` slice of a label that the search query matched, for the
 *  view to render bold. */
export interface MatchRange {
  start: number;
  end: number;
}

/** The least a row has to carry to be searchable: what it is called and what
 *  it is. Rows keep whatever else they hold -- searchMembers hands the row
 *  itself back. */
export interface SearchableMember {
  label: string;
  kind: MemberKind;
}

export interface MemberSearchResult<T extends SearchableMember> {
  member: T;
  // Where the query hit the label, left to right and never overlapping. Empty
  // when the row was kept on its kind alone, so nothing renders bold.
  labelRanges: MatchRange[];
}

/**
 * Filter the sidebar's tab list to the rows a query matches.
 *
 * A row is kept when the query appears in its label or in its kind, so typing
 * "browser" keeps every browser however its tab happens to be titled. Matching
 * is case-insensitive and on the raw substring, which is what lets the ranges
 * come back as label offsets: the view bolds exactly what the user typed
 * rather than re-deriving it. An empty query keeps everything with nothing
 * bolded.
 */
export function searchMembers<T extends SearchableMember>(
  members: readonly T[],
  query: string,
): MemberSearchResult<T>[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return members.map((member) => ({ member, labelRanges: [] }));
  const results: MemberSearchResult<T>[] = [];
  for (const member of members) {
    const labelRanges: MatchRange[] = [];
    const haystack = member.label.toLowerCase();
    let searchFrom = 0;
    let start = haystack.indexOf(needle, searchFrom);
    while (start !== -1) {
      labelRanges.push({ start, end: start + needle.length });
      searchFrom = start + needle.length;
      start = haystack.indexOf(needle, searchFrom);
    }
    if (labelRanges.length > 0 || member.kind.includes(needle)) {
      results.push({ member, labelRanges });
    }
  }
  return results;
}
