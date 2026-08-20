/**
 * Row height measurement for the virtualized transcript: what a row measured,
 * whether that measurement can be trusted yet, and when it stopped changing.
 *
 * Kept separate from the virtualizer because the two answer different questions.
 * The virtualizer wants "how tall is row N right now" so it can window; the
 * geometry cache wants "is this height final" so it can persist it. Conflating
 * them is how a half-rendered height ends up remembered forever.
 *
 * Rows are addressed by their DOM ``id``, which is the virtualization row key --
 * the same contract the selection code walks the DOM for. Nothing here needs a
 * separate index attribute, so the message renderers are untouched.
 *
 * Two behaviours are load-bearing:
 *
 * **Hysteresis.** A measured height must differ from the accepted one by more
 * than a pixel to count. Without it, a row sitting at a fractional vertical
 * offset reflows by a fraction each frame, that fraction reads as a change, and
 * the redraw it schedules shifts the row again -- a continuous ~1px jitter that
 * never settles. Sub-threshold observations return the *accepted* value, not the
 * observed one, so repeated wobble cannot walk the cache across the threshold a
 * fraction at a time.
 *
 * **Settling.** A row is trustworthy only once it has gone quiet for
 * ``SETTLE_QUIET_MS``. Markdown, syntax highlighting and images all land after
 * first paint, so the height at mount is routinely not the final height.
 * Persisting eagerly would poison the cache with a placeholder height that then
 * survives reloads.
 */

/** A height must differ from the accepted value by MORE than this to count. */
export const MEASURE_HYSTERESIS_PX = 1;

/** How long a row must go unchanged before its height is considered final. */
export const SETTLE_QUIET_MS = 500;

export interface MeasuredRow {
  height: number;
  /** Whether the row has gone quiet long enough to be worth persisting. */
  is_settled: boolean;
}

export interface RowMeasurementStore {
  /**
   * Record an observed height. Returns the accepted height, which is the prior
   * value when the delta is within the hysteresis threshold.
   */
  observe(rowKey: string, observedHeight: number): number;
  /** Accepted height for a row, or undefined if never measured. */
  heightFor(rowKey: string): number | undefined;
  /** Whether a row's height has gone quiet long enough to persist. */
  isSettled(rowKey: string, now: number): boolean;
  /** Drop rows no longer present once the store drifts past the live set. */
  prune(liveKeys: Set<string>): void;
  /** Forget everything (switching to a different agent, or a width change). */
  reset(): void;
}

interface Entry {
  height: number;
  /** Timestamp of the last accepted change, for the settle window. */
  changed_at: number;
}

/**
 * Slack before pruning, so an ordinary redraw does not walk the whole map. Rows
 * churn constantly as the window moves; only a genuine drift is worth cleaning.
 */
const PRUNE_SLACK_ENTRIES = 256;

export function createRowMeasurementStore(now: () => number = () => Date.now()): RowMeasurementStore {
  let entries = new Map<string, Entry>();

  return {
    observe(rowKey: string, observedHeight: number): number {
      const prior = entries.get(rowKey);
      // A row that is not laid out yet reads as zero. Recording that would
      // collapse the geometry, so keep whatever is known instead.
      if (observedHeight <= 0) {
        return prior?.height ?? 0;
      }
      if (prior !== undefined && Math.abs(observedHeight - prior.height) <= MEASURE_HYSTERESIS_PX) {
        return prior.height;
      }
      entries.set(rowKey, { height: observedHeight, changed_at: now() });
      return observedHeight;
    },

    heightFor: (rowKey: string) => entries.get(rowKey)?.height,

    isSettled(rowKey: string, at: number): boolean {
      const entry = entries.get(rowKey);
      return entry !== undefined && at - entry.changed_at >= SETTLE_QUIET_MS;
    },

    prune(liveKeys: Set<string>): void {
      if (entries.size <= liveKeys.size + PRUNE_SLACK_ENTRIES) {
        return;
      }
      for (const key of [...entries.keys()]) {
        if (!liveKeys.has(key)) {
          entries.delete(key);
        }
      }
    },

    reset(): void {
      entries = new Map<string, Entry>();
    },
  };
}

/**
 * Read every mounted row's height out of the list and feed it to the store,
 * reporting the keys whose accepted height changed.
 *
 * Uses ``getBoundingClientRect().height`` rather than ``offsetHeight``: the
 * latter is an integer snapped to device pixels and therefore depends on the
 * row's fractional vertical position, so it flips by a pixel as the row drifts
 * -- the other half of the jitter this module exists to prevent.
 *
 * Spacers carry no ``id`` and are skipped.
 */
export function measureMountedRows(listElement: Element, store: RowMeasurementStore): Map<string, number> {
  const changed = new Map<string, number>();
  for (const child of Array.from(listElement.children)) {
    const element = child as HTMLElement;
    const rowKey = element.id;
    if (rowKey === "") {
      continue;
    }
    const observed = element.getBoundingClientRect().height;
    if (observed <= 0) {
      continue;
    }
    const before = store.heightFor(rowKey);
    const accepted = store.observe(rowKey, observed);
    if (before !== accepted) {
      changed.set(rowKey, accepted);
    }
  }
  return changed;
}
