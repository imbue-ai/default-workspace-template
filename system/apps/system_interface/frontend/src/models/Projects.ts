/**
 * API client + pure helpers for projects.
 *
 * A project is a named, server-persisted dockview state plus the display
 * metadata the picker and sidebar render (name, color, squiggle glyph).
 * Membership is implicit: a tab belongs to a project exactly when a panel for
 * it exists in that project's saved content, so there is no separate
 * membership map to keep in sync.
 *
 * "Everything" is a real stored project that always exists and cannot be
 * deleted; new tabs are written into it as well as into the active project,
 * which is what keeps it unfiltered while still holding its own arrangement.
 * See DockviewWorkspace for the consuming logic.
 */

import { apiUrl } from "../base-path";

export const EVERYTHING_PROJECT_ID = "everything";

export interface ProjectInfo {
  project_id: string;
  name: string;
  color: string;
  glyph: number;
  has_content: boolean;
}

export interface ProjectsListResponse {
  projects: ProjectInfo[];
  last_active_id: string | null;
}

/** Fetch the project registry. Defensive: an unreachable server yields an
 *  empty list so the workspace still renders (nothing will persist). */
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

/** Fetch one project's saved content. Returns null both for an empty project
 *  (never saved yet -- render the fresh welcome-chat state) and on any fetch
 *  failure. */
export async function fetchProjectContent(projectId: string): Promise<unknown | null> {
  try {
    const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(projectId)}`));
    if (!response.ok) return null;
    const data = (await response.json()) as { layout?: unknown };
    return data.layout ?? null;
  } catch {
    return null;
  }
}

async function errorDetailFromResponse(response: Response): Promise<string> {
  const data = (await response.json().catch(() => ({}))) as { detail?: string };
  return data.detail ?? `HTTP ${response.status}`;
}

/** Autosave the active project's content. Throws on failure (callers treat
 *  autosave as best-effort and catch). */
export async function autosaveProject(projectId: string, layoutPayload: unknown, clientId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(projectId)}`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ layout: layoutPayload, client_id: clientId }),
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
 *  project's saved content is untouched. Throws with the server's detail on
 *  rejection (unknown project, bad name, bad color or glyph). */
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

/** Delete a project. Throws with the server's detail on rejection (unknown
 *  project, or the Everything project, which may never be deleted). */
export async function deleteProjectRequest(projectId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/projects/${encodeURIComponent(projectId)}/delete`), { method: "POST" });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
}

/**
 * Drop one panel from every project that holds it, returning the ids that
 * changed.
 *
 * This is the storage half of destroying a tab. Closing only removes a tab
 * from the project you are looking at, but destroying tears down the agent,
 * terminal, or browser behind it, so the panel has to leave the projects that
 * are not currently mounted as well -- otherwise switching to one of them
 * would restore a tab whose identity can no longer be resolved. Best-effort:
 * a failure here must not block the destroy itself, so callers catch.
 */
export async function removePanelFromAllProjects(panelId: string): Promise<string[]> {
  const response = await fetch(apiUrl(`/api/projects/panels/${encodeURIComponent(panelId)}/delete`), {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  const data = (await response.json()) as { project_ids?: string[] };
  return data.project_ids ?? [];
}

/**
 * Pick the project a client should start on: its stored per-browser choice
 * when that project still exists, else Everything (which the server always
 * keeps), else the first project. Null only when no projects exist at all,
 * which means the registry could not be read.
 */
export function chooseInitialProject(projects: ProjectInfo[], storedId: string): ProjectInfo | null {
  if (projects.length === 0) return null;
  const stored = projects.find((project) => project.project_id === storedId);
  if (stored) return stored;
  const everything = projects.find((project) => project.project_id === EVERYTHING_PROJECT_ID);
  if (everything) return everything;
  return projects[0];
}
