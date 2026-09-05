/**
 * The projects: the shell's REST routes for them (contracts.md section 6) and the pure rules
 * the views apply over them.
 *
 * A project is a **view**: a shared tab set (the addresses it shows) plus each client's own
 * arrangement of it, and a rail of shortcuts. The tab set is many-to-many: the same instance
 * can sit in any number of projects, and nothing owns anything. Opening an instance in a project
 * files it there; closing a tab changes nothing; an instance leaves every tab set only when its
 * app stops listing it.
 *
 * "Everything" is the view with no filter and the home: every instance on the machine is in it.
 * It has an arrangement per client like any other view but no registry entry -- it never comes
 * back from `fetchProjectsList`, has no tab set of its own, and cannot be renamed or deleted.
 */

import { apiUrl } from "../base-path";
import { postJson } from "./http";
import type { ProjectInfo, ProjectShortcut, ShortcutMode } from "./Inventory";

/** The id of the reserved unfiltered view, matching the backend's ``EVERYTHING_VIEW_ID``. */
export const EVERYTHING_VIEW_ID = "everything";

/** The display name of the unfiltered view, matching the backend's ``EVERYTHING_VIEW_NAME``. */
export const EVERYTHING_VIEW_NAME = "Everything";

/** Whether a view id addresses the unfiltered view rather than a project. */
export function isEverythingView(viewId: string): boolean {
  return viewId === EVERYTHING_VIEW_ID;
}

/** Fetch the project registry. Everything is never in it. Defensive: an unreachable server
 *  yields an empty list so the workspace still renders (nothing will persist). */
export async function fetchProjectsList(): Promise<ProjectInfo[]> {
  try {
    const response = await fetch(apiUrl("/api/projects"));
    if (!response.ok) return [];
    const data = (await response.json()) as { projects?: ProjectInfo[] };
    return data.projects ?? [];
  } catch {
    return [];
  }
}

/** Create an empty project with the given display metadata. Its rail is seeded server-side
 *  from every app's ``default_shortcut``. Throws with the server's detail on rejection. */
export async function createProject(name: string, color: string, glyph: number): Promise<ProjectInfo> {
  return postJson<ProjectInfo>(apiUrl("/api/projects"), { name, color, glyph });
}

/** Rename/restyle an existing project. The id is stable across edits, so the project's tab set
 *  and arrangements are untouched. Throws with the server's detail on rejection. */
export async function updateProjectSettings(
  projectId: string,
  name: string,
  color: string,
  glyph: number,
): Promise<ProjectInfo> {
  return postJson<ProjectInfo>(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/settings`), {
    name,
    color,
    glyph,
  });
}

/** Delete a project: its registry entry and its arrangements go, and nothing else happens --
 *  every instance it showed keeps running and stays wherever else it is shown. Answers the view
 *  clients on it fall back to. Throws with the server's detail on rejection. */
export async function deleteProjectRequest(projectId: string): Promise<string> {
  const data = await postJson<{ fallback_view_id: string }>(
    apiUrl(`/api/projects/${encodeURIComponent(projectId)}/delete`),
    {},
  );
  return data.fallback_view_id;
}

/** Show an address in a project. Idempotent, and indifferent to what else shows it. */
export async function addProjectTab(projectId: string, address: string): Promise<ProjectInfo> {
  return postJson<ProjectInfo>(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/tabs`), { address });
}

/** Stop showing an address in one project, leaving the instance itself alone. */
export async function removeProjectTab(projectId: string, address: string): Promise<ProjectInfo> {
  return postJson<ProjectInfo>(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/tabs/remove`), { address });
}

/** Add a shortcut to a project's rail, or change the mode of the one for the same (app, action). */
export async function setProjectShortcut(
  projectId: string,
  app: string,
  action: string,
  mode: ShortcutMode,
): Promise<ProjectInfo> {
  return postJson<ProjectInfo>(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/shortcuts`), {
    app,
    action,
    mode,
  });
}

/** Take a shortcut off a project's rail. */
export async function removeProjectShortcut(projectId: string, app: string, action: string): Promise<ProjectInfo> {
  return postJson<ProjectInfo>(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/shortcuts/remove`), {
    app,
    action,
  });
}

/**
 * Pick the view a client should mount on: its stored per-browser choice when that view still
 * exists, else the first project, else Everything.
 *
 * A machine may genuinely have zero projects, and a registry that could not be read looks the
 * same as one holding none -- either way Everything is always there to land on.
 */
export function chooseInitialViewId(projects: readonly ProjectInfo[], storedId: string): string {
  if (isEverythingView(storedId)) return EVERYTHING_VIEW_ID;
  const stored = projects.find((project) => project.id === storedId);
  if (stored) return stored.id;
  return projects.length === 0 ? EVERYTHING_VIEW_ID : projects[0].id;
}

/** The project backing a view id, or null for Everything and for a project since deleted. */
export function projectForViewId(projects: readonly ProjectInfo[], viewId: string): ProjectInfo | null {
  return projects.find((project) => project.id === viewId) ?? null;
}

/** What to call a view in a message to the user: Everything by name, a project by its name,
 *  and a project the registry no longer holds by its id. */
export function displayNameForView(viewId: string, projects: readonly ProjectInfo[]): string {
  if (isEverythingView(viewId)) return EVERYTHING_VIEW_NAME;
  return projectForViewId(projects, viewId)?.name ?? viewId;
}

/** The shortcut a project keeps for one (app, action), or null. */
export function findShortcut(project: ProjectInfo | null, app: string, action: string): ProjectShortcut | null {
  if (project === null) return null;
  return project.shortcuts.find((shortcut) => shortcut.app === app && shortcut.action === action) ?? null;
}

/** A `[start, end)` slice of a label that the search query matched, for the view to render bold. */
export interface MatchRange {
  start: number;
  end: number;
}

/** The least a row has to carry to be searchable: what it is called, and the words that say
 *  what it is (its app's names), so typing "terminal" keeps every terminal however titled. */
export interface SearchableRow {
  label: string;
  kindWords: readonly string[];
}

export interface RowSearchResult<T extends SearchableRow> {
  row: T;
  // Where the query hit the label, left to right and never overlapping. Empty when the row was
  // kept on its kind alone, so nothing renders bold.
  labelRanges: MatchRange[];
}

/**
 * Filter a tab list to the rows a query matches: those whose label contains it, or whose kind
 * words do. Case-insensitive, on the raw substring, so the ranges come back as label offsets.
 * An empty query keeps everything with nothing bolded.
 */
export function searchRows<T extends SearchableRow>(rows: readonly T[], query: string): RowSearchResult<T>[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return rows.map((row) => ({ row, labelRanges: [] }));
  const results: RowSearchResult<T>[] = [];
  for (const row of rows) {
    const labelRanges: MatchRange[] = [];
    const haystack = row.label.toLowerCase();
    let start = haystack.indexOf(needle);
    while (start !== -1) {
      labelRanges.push({ start, end: start + needle.length });
      start = haystack.indexOf(needle, start + needle.length);
    }
    if (labelRanges.length > 0 || row.kindWords.some((word) => word.toLowerCase().includes(needle))) {
      results.push({ row, labelRanges });
    }
  }
  return results;
}
