/**
 * The DOM the shell's view tests need beyond jsdom: the library's requestAnimationFrame polyfill
 * (imported first, before mithril reads the global) and a ResizeObserver, since the App view
 * measures the backdrop area through one; the tests size the backdrop through the store
 * (``setBackdropSize``) instead.
 */

import "@imbue/workspace-ui/src/testing/dom";

globalThis.ResizeObserver ??= class {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
} as unknown as typeof globalThis.ResizeObserver;
