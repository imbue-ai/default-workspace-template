/**
 * Offscreen row measurement: renders not-yet-measured transcript rows into a
 * hidden container (same classes and width as the live message list, so layout
 * is identical) in idle-time batches, and reports their exact heights. This is
 * how the physical layer's geometry becomes fully measured without mounting
 * 50k rows in the live DOM.
 *
 * The container lives outside the scroll element (a hidden absolutely
 * positioned box clipped to zero height), so measuring never perturbs
 * scrollHeight or native scrolling.
 */

import m from "mithril";
import { MESSAGE_LIST_CLASS, type RowDescriptor } from "./conversation-rows";

// Per-batch layout budget. Rendering + measuring runs until the budget is
// spent, then yields to the next idle callback so streaming and input stay
// responsive while a large fill measures.
const BATCH_BUDGET_MS = 8;
// Upper bound on waiting for an idle slot; measurement matters for scrollbar
// accuracy, so it should not starve on a busy tab.
const IDLE_TIMEOUT_MS = 300;

export interface OffscreenMeasurer {
  /** Queue rows for measurement (deduped by key); starts the idle loop. */
  requestMeasure(rows: readonly RowDescriptor[]): void;
  /** Drop the queue and tear down the hidden container. */
  cancel(): void;
}

export interface OffscreenMeasurerOptions {
  /** Element the hidden container is created under (the panel root, not the scroll element). */
  getHostEl: () => HTMLElement | null;
  /** Live message-list content width; measurement is deferred while unknown. */
  getListWidthPx: () => number | null;
  /** Receives each batch's measured heights (by row key). */
  onHeights: (heightByRowKey: Map<string, number>) => void;
}

function scheduleIdle(callback: () => void): void {
  if (typeof requestIdleCallback === "function") {
    requestIdleCallback(() => callback(), { timeout: IDLE_TIMEOUT_MS });
  } else {
    setTimeout(callback, 16);
  }
}

export function createOffscreenMeasurer(options: OffscreenMeasurerOptions): OffscreenMeasurer {
  const queue: RowDescriptor[] = [];
  const queuedKeys = new Set<string>();
  let isScheduled = false;
  let wrapperEl: HTMLElement | null = null;
  let listEl: HTMLElement | null = null;

  function ensureContainer(hostEl: HTMLElement, widthPx: number): HTMLElement {
    if (wrapperEl === null || listEl === null || wrapperEl.parentElement !== hostEl) {
      teardownContainer();
      wrapperEl = document.createElement("div");
      // Hidden and clipped to zero height: children lay out at full width for
      // measurement but paint nothing and take no space.
      wrapperEl.style.cssText = "position:absolute;top:0;left:0;height:0;overflow:hidden;visibility:hidden;";
      wrapperEl.setAttribute("aria-hidden", "true");
      listEl = document.createElement("div");
      listEl.className = MESSAGE_LIST_CLASS;
      wrapperEl.appendChild(listEl);
      hostEl.appendChild(wrapperEl);
    }
    wrapperEl.style.width = `${widthPx}px`;
    return listEl!;
  }

  function teardownContainer(): void {
    if (listEl !== null) {
      m.render(listEl, []);
    }
    wrapperEl?.remove();
    wrapperEl = null;
    listEl = null;
  }

  function runBatch(): void {
    isScheduled = false;
    if (queue.length === 0) {
      teardownContainer();
      return;
    }
    const hostEl = options.getHostEl();
    const widthPx = options.getListWidthPx();
    if (hostEl === null || widthPx === null || widthPx <= 0) {
      // Not mounted or width unknown yet; retry on the next idle slot.
      scheduleNext();
      return;
    }
    const containerEl = ensureContainer(hostEl, widthPx);

    const heightByRowKey = new Map<string, number>();
    const startedAtMs = performance.now();
    while (queue.length > 0 && performance.now() - startedAtMs < BATCH_BUDGET_MS) {
      const row = queue.shift()!;
      queuedKeys.delete(row.key);
      m.render(containerEl, [row.render()]);
      const rowEl = containerEl.firstElementChild;
      if (rowEl instanceof HTMLElement) {
        // Outer pitch, matching the live measurer: border-box height plus the
        // row's own margins (flex column, so margins never collapse and the
        // pitch is per-row deterministic).
        const style = getComputedStyle(rowEl);
        const pitchPx =
          rowEl.getBoundingClientRect().height +
          (parseFloat(style.marginTop) || 0) +
          (parseFloat(style.marginBottom) || 0);
        if (pitchPx > 0) {
          heightByRowKey.set(row.key, pitchPx);
        }
      }
    }
    // Unmount through mithril so component lifecycles (onremove) run.
    m.render(containerEl, []);

    if (heightByRowKey.size > 0) {
      options.onHeights(heightByRowKey);
    }
    if (queue.length > 0) {
      scheduleNext();
    } else {
      teardownContainer();
    }
  }

  function scheduleNext(): void {
    if (!isScheduled) {
      isScheduled = true;
      scheduleIdle(runBatch);
    }
  }

  return {
    requestMeasure(rows: readonly RowDescriptor[]): void {
      for (const row of rows) {
        if (!queuedKeys.has(row.key)) {
          queuedKeys.add(row.key);
          queue.push(row);
        }
      }
      if (queue.length > 0) {
        scheduleNext();
      }
    },

    cancel(): void {
      queue.length = 0;
      queuedKeys.clear();
      teardownContainer();
    },
  };
}
