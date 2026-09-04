/**
 * Dev-only scroll trace: a fixed-capacity ring buffer of structured entries
 * (state transitions, anchor resolutions, compensation writes, spacer and fill
 * decisions), dumpable as JSON. The last scroll investigation showed
 * scrollTop-only tracing is blind to the real jumps; the engine records
 * content-relative facts here instead. Enablement (?debug=scroll) and exposure
 * on `window` are the engine's concern -- this module is pure bookkeeping.
 */

export interface ScrollTraceEntry {
  readonly atMs: number;
  readonly kind: string;
  readonly detail: Record<string, unknown>;
}

export interface ScrollTrace {
  record(kind: string, detail: Record<string, unknown>): void;
  /** Entries in chronological order (oldest first). */
  entries(): ScrollTraceEntry[];
  clear(): void;
}

export interface ScrollTraceOptions {
  readonly capacityEntryCount: number;
  readonly now: () => number;
  /** Optional immediate echo of each entry (e.g. console.debug). */
  readonly echo: ((entry: ScrollTraceEntry) => void) | null;
}

export function createScrollTrace(options: ScrollTraceOptions): ScrollTrace {
  const buffer: ScrollTraceEntry[] = [];
  let totalRecordedCount = 0;

  return {
    record(kind: string, detail: Record<string, unknown>): void {
      const entry: ScrollTraceEntry = { atMs: options.now(), kind, detail };
      if (buffer.length < options.capacityEntryCount) {
        buffer.push(entry);
      } else {
        buffer[totalRecordedCount % options.capacityEntryCount] = entry;
      }
      totalRecordedCount += 1;
      if (options.echo !== null) {
        options.echo(entry);
      }
    },

    entries(): ScrollTraceEntry[] {
      if (buffer.length < options.capacityEntryCount) {
        return buffer.slice();
      }
      const splitAt = totalRecordedCount % options.capacityEntryCount;
      return [...buffer.slice(splitAt), ...buffer.slice(0, splitAt)];
    },

    clear(): void {
      buffer.length = 0;
      totalRecordedCount = 0;
    },
  };
}
