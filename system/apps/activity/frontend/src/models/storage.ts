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

export async function measureStorage(): Promise<void> {
  if (state.kind === "measuring") return;
  state = { kind: "measuring", previous: state.kind === "loaded" ? state.summary : null };
  m.redraw();
  try {
    const response = await fetch(STORAGE_PATH, { cache: "no-store" });
    state = response.ok
      ? { kind: "loaded", summary: (await response.json()) as StorageSummary }
      : { kind: "failed", message: `The page answered ${response.status}.` };
  } catch (error) {
    state = { kind: "failed", message: String(error) };
  } finally {
    m.redraw();
  }
}
