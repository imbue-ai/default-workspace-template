/**
 * The shell's desktop routes (desktop-interface contracts.md section 5), as the frontend calls
 * them: every function posts or fetches one route and answers the parsed record, throwing with
 * the shell's own ``detail`` on a refusal. A placements save refused as stale throws
 * ``StalePlacementsSaveError`` so the caller can refetch rather than retry.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { HttpError, errorDetailFromResponse, postJson } from "@imbue/workspace-ui/src/models/http";
import {
  parseClientRecord,
  parseClientRecords,
  parseDesktop,
  parseDesktops,
  parseLayout,
  parseWallpaperListings,
  parseWindow,
} from "./records";
import type {
  ClientRecord,
  Desktop,
  DesktopShortcut,
  EntryPresentation,
  GridCell,
  IfPresent,
  Layout,
  Placement,
  SharingMode,
  Wallpaper,
  WallpaperListing,
  WindowRecord,
} from "./records";

const HTTP_CONFLICT = 409;

/** The shell refused a placements save because the stored layout is newer than the one it was based on. */
export class StalePlacementsSaveError extends Error {}

async function getJson(url: string): Promise<unknown> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await errorDetailFromResponse(response));
  return (await response.json()) as unknown;
}

function desktopUrl(desktopId: string, suffix: string = ""): string {
  return apiUrl(`/api/desktops/${encodeURIComponent(desktopId)}${suffix}`);
}

export async function fetchDesktops(): Promise<Desktop[]> {
  const data = (await getJson(apiUrl("/api/desktops"))) as { desktops?: unknown };
  return parseDesktops(data.desktops);
}

export async function createDesktop(name: string, color: string, glyph: number): Promise<Desktop> {
  return parseDesktop(await postJson<unknown>(apiUrl("/api/desktops"), { name, color, glyph }));
}

export async function updateDesktopSettings(
  desktopId: string,
  name: string,
  color: string,
  glyph: number,
  sharing: SharingMode,
): Promise<Desktop> {
  return parseDesktop(await postJson<unknown>(desktopUrl(desktopId, "/settings"), { name, color, glyph, sharing }));
}

export async function setDesktopWallpaper(desktopId: string, wallpaper: Wallpaper | null): Promise<Desktop> {
  return parseDesktop(await postJson<unknown>(desktopUrl(desktopId, "/wallpaper"), { wallpaper }));
}

/** Delete a desktop; answers the desktop its clients fall back to. The last desktop is refused. */
export async function deleteDesktop(desktopId: string): Promise<string> {
  const data = await postJson<{ fallback_desktop_id: string }>(desktopUrl(desktopId, "/delete"), {});
  return data.fallback_desktop_id;
}

export async function setDesktopShortcut(desktopId: string, shortcut: DesktopShortcut): Promise<Desktop> {
  return parseDesktop(await postJson<unknown>(desktopUrl(desktopId, "/shortcuts"), shortcut));
}

export async function moveDesktopShortcut(
  desktopId: string,
  app: string,
  launch: string,
  cell: GridCell,
): Promise<Desktop> {
  return parseDesktop(await postJson<unknown>(desktopUrl(desktopId, "/shortcuts/move"), { app, launch, cell }));
}

export async function removeDesktopShortcut(desktopId: string, app: string, launch: string): Promise<Desktop> {
  return parseDesktop(await postJson<unknown>(desktopUrl(desktopId, "/shortcuts/remove"), { app, launch }));
}

export interface WindowOpenRequest {
  readonly app: string;
  readonly path: string;
  readonly clientId: string;
  readonly ifPresent: IfPresent;
  /** The launch path the path was built from, when it was; it marks the window as settling. */
  readonly launch: string | null;
}

export interface WindowOpenOutcome {
  readonly window: WindowRecord;
  /** True for an open, false when an existing window was answered and raised instead. */
  readonly isNew: boolean;
}

export async function openWindow(desktopId: string, request: WindowOpenRequest): Promise<WindowOpenOutcome> {
  const body: Record<string, unknown> = {
    app: request.app,
    path: request.path,
    client_id: request.clientId,
    if_present: request.ifPresent,
  };
  if (request.launch !== null) body.launch = request.launch;
  const data = await postJson<{ window: unknown; is_new: boolean }>(desktopUrl(desktopId, "/windows"), body);
  return { window: parseWindow(data.window), isNew: data.is_new === true };
}

export async function closeWindow(desktopId: string, windowId: string): Promise<void> {
  await postJson<void>(desktopUrl(desktopId, `/windows/${encodeURIComponent(windowId)}/close`), {});
}

/** Report where a page is; the answer is the window as this client sees it (an independent window at the
 *  client's own path). */
export async function reportWindowLocation(
  desktopId: string,
  windowId: string,
  clientId: string,
  path: string,
  title: string,
): Promise<WindowRecord> {
  return parseWindow(
    await postJson<unknown>(desktopUrl(desktopId, `/windows/${encodeURIComponent(windowId)}/location`), {
      client_id: clientId,
      path,
      title,
    }),
  );
}

export async function fetchPlacements(desktopId: string, clientId: string): Promise<Layout> {
  const query = `client=${encodeURIComponent(clientId)}`;
  return parseLayout(await getJson(apiUrl(`/api/placements/${encodeURIComponent(desktopId)}?${query}`)));
}

export interface PlacementsSaveRequest {
  readonly clientId: string;
  readonly saveId: string;
  readonly baseUpdatedAt: string | null;
  readonly placements: readonly Placement[];
}

/** Save this client's layout of a desktop; answers the stamp written, or null when nothing changed. */
export async function savePlacements(desktopId: string, request: PlacementsSaveRequest): Promise<string | null> {
  let data: { updated_at: string | null };
  try {
    data = await postJson<{ updated_at: string | null }>(apiUrl(`/api/placements/${encodeURIComponent(desktopId)}`), {
      client_id: request.clientId,
      save_id: request.saveId,
      base_updated_at: request.baseUpdatedAt,
      placements: request.placements,
    });
  } catch (error) {
    if (error instanceof HttpError && error.status === HTTP_CONFLICT)
      throw new StalePlacementsSaveError(error.message);
    throw error;
  }
  return data.updated_at ?? null;
}

export async function fetchClients(): Promise<ClientRecord[]> {
  const data = (await getJson(apiUrl("/api/clients"))) as { clients?: unknown };
  return parseClientRecords(data.clients);
}

/** Write how this client shows one pinned entry; answers the client record. */
export async function setEntryPresentation(
  clientId: string,
  app: string,
  presentation: EntryPresentation,
): Promise<ClientRecord> {
  return parseClientRecord(
    await postJson<unknown>(
      apiUrl(`/api/clients/${encodeURIComponent(clientId)}/entries/${encodeURIComponent(app)}`),
      presentation,
    ),
  );
}

export async function fetchWallpapers(): Promise<WallpaperListing[]> {
  const data = (await getJson(apiUrl("/api/wallpapers"))) as { wallpapers?: unknown };
  return parseWallpaperListings(data.wallpapers);
}

/** Where a wallpaper reference's image is served. */
export function wallpaperImageUrl(wallpaper: Wallpaper): string {
  return apiUrl(`/wallpapers/${wallpaper.kind}/${encodeURIComponent(wallpaper.name)}`);
}

export type AppLifecycleAction = "stop" | "start";

/** Stop or start an app's supervised program; the ``apps_updated`` push that follows carries the result. */
export async function setAppLifecycle(appName: string, action: AppLifecycleAction): Promise<void> {
  await postJson<void>(apiUrl(`/api/apps/${encodeURIComponent(appName)}/${action}`), {});
}
