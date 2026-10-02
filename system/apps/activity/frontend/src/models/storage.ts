/**
 * The storage tab's data: the backend's ``GET /api/storage`` document (``activity.storage.StorageSummary``). It is
 * measured when the tab opens and when the user asks again, never on a timer: ``du`` walks every file.
 */

import m from "mithril";

export interface MeasuredFolder {
  readonly path: string;
  readonly size_kib: number;
}

export interface MeasuredCategory {
  readonly category_id: string;
  readonly name: string;
  readonly description: string;
  readonly size_kib: number;
  readonly folders: readonly MeasuredFolder[];
}

export interface LargestFolder {
  readonly path: string;
  readonly size_kib: number;
  readonly category_name: string;
}

export interface StorageSummary {
  readonly measured_at: string;
  readonly measure_seconds: number;
  readonly total_kib: number;
  readonly categories: readonly MeasuredCategory[];
  readonly largest: readonly LargestFolder[];
  readonly command: string;
  readonly notes: readonly string[];
}

export type StorageState =
  | { readonly kind: "idle" }
  | { readonly kind: "measuring"; readonly previous: StorageSummary | null }
  | { readonly kind: "loaded"; readonly summary: StorageSummary }
  | { readonly kind: "failed"; readonly message: string };

export const STORAGE_PATH = "/api/storage";

let state: StorageState = { kind: "idle" };

export function getStorageState(): StorageState {
  return state;
}

// du can take its full minute on a big workspace under gVisor; past the backend's own limit, give up.
export const STORAGE_TIMEOUT_MS = 75_000;
const HTTP_FORBIDDEN = 403;
const HTTP_TOO_MANY_REQUESTS = 429;

export async function measureStorage(): Promise<void> {
  if (state.kind === "measuring") return;
  const previous = state.kind === "loaded" ? state.summary : null;
  state = { kind: "measuring", previous };
  m.redraw();
  try {
    const response = await fetch(STORAGE_PATH, { cache: "no-store", signal: AbortSignal.timeout(STORAGE_TIMEOUT_MS) });
    if (response.status === HTTP_TOO_MANY_REQUESTS) {
      // Another window (or an agent) is measuring right now: keep what is on screen rather than call it a failure.
      state =
        previous !== null
          ? { kind: "loaded", summary: previous }
          : { kind: "failed", message: "Another window is measuring right now; try again in a moment." };
    } else if (response.status === HTTP_FORBIDDEN) {
      state = { kind: "failed", message: "System Monitor is only available to the workspace's owner." };
    } else {
      state = response.ok
        ? { kind: "loaded", summary: (await response.json()) as StorageSummary }
        : { kind: "failed", message: `The page answered ${response.status}.` };
    }
  } catch (error) {
    state = { kind: "failed", message: String(error) };
  } finally {
    m.redraw();
  }
}
