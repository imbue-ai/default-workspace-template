/**
 * Codec for the persisted per-agent scroll state (localStorage). Decoding is
 * defensive: anything malformed degrades to FOLLOW rather than throwing, since
 * a stale or corrupt entry must never break opening a chat. The anchor's row
 * key can only be validated once the physical window has loaded, so that check
 * is a separate step (`validateRestoredAnchor`) the engine runs after the
 * restore-centered fill lands.
 */

import { FOLLOW_STATE } from "./state";
import type { EventIndex, PersistedScrollState, RowKey, ScrollPositionState } from "./types";

const PERSISTED_VERSION = 1;

export interface RestoredScrollState {
  readonly state: ScrollPositionState;
  readonly anchorEventIndex: EventIndex | null;
}

export const FOLLOW_RESTORED: RestoredScrollState = { state: FOLLOW_STATE, anchorEventIndex: null };

export function scrollStateStorageKey(agentId: string): string {
  return `transcript-scroll:${agentId}`;
}

export function encodePersistedScrollState(state: ScrollPositionState, anchorEventIndex: EventIndex | null): string {
  const persisted: PersistedScrollState = { version: PERSISTED_VERSION, state, anchorEventIndex };
  return JSON.stringify(persisted);
}

function isValidAnchor(value: unknown): value is { rowKey: string; offsetPx: number } {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const anchor = value as { rowKey?: unknown; offsetPx?: unknown };
  return typeof anchor.rowKey === "string" && anchor.rowKey.length > 0 && Number.isFinite(anchor.offsetPx);
}

export function decodePersistedScrollState(raw: string | null): RestoredScrollState {
  if (raw === null) {
    return FOLLOW_RESTORED;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return FOLLOW_RESTORED;
  }
  if (typeof parsed !== "object" || parsed === null) {
    return FOLLOW_RESTORED;
  }
  const persisted = parsed as { version?: unknown; state?: unknown; anchorEventIndex?: unknown };
  if (persisted.version !== PERSISTED_VERSION || typeof persisted.state !== "object" || persisted.state === null) {
    return FOLLOW_RESTORED;
  }
  const state = persisted.state as { kind?: unknown; anchor?: unknown };
  const anchorEventIndex =
    typeof persisted.anchorEventIndex === "number" &&
    Number.isFinite(persisted.anchorEventIndex) &&
    persisted.anchorEventIndex >= 0
      ? Math.floor(persisted.anchorEventIndex)
      : null;
  if (state.kind === "FOLLOW") {
    return { state: FOLLOW_STATE, anchorEventIndex: null };
  }
  if (state.kind === "USER_CONTROLLED" && isValidAnchor(state.anchor)) {
    return {
      state: { kind: "USER_CONTROLLED", anchor: { rowKey: state.anchor.rowKey, offsetPx: state.anchor.offsetPx } },
      anchorEventIndex,
    };
  }
  return FOLLOW_RESTORED;
}

/** Downgrade a restored USER_CONTROLLED state to FOLLOW when its row no longer exists. */
export function validateRestoredAnchor(
  restored: RestoredScrollState,
  isRowKeyKnown: (key: RowKey) => boolean,
): ScrollPositionState {
  if (restored.state.kind === "USER_CONTROLLED" && !isRowKeyKnown(restored.state.anchor.rowKey)) {
    return FOLLOW_STATE;
  }
  return restored.state;
}
