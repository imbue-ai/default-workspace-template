/**
 * The DOM a mithril view test needs beyond jsdom: a requestAnimationFrame for mithril's redraw
 * scheduling. Imported before mithril, as the first import of a test file, so the polyfill is in
 * place when mithril reads the global. Kept apart from ``mount.ts``, which imports mithril.
 */

globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
  setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
