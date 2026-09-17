/**
 * The DOM the view tests need beyond jsdom: a requestAnimationFrame for mithril's redraw
 * scheduling. Imported before mithril, as the first import of a test file, so the polyfill is
 * in place when mithril reads the global.
 */

globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
  setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;

// dockview-core watches its container's size through a ResizeObserver, which jsdom does not
// provide; a dock built in a test is sized by an explicit ``layout(width, height)`` instead.
globalThis.ResizeObserver ??= class {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
} as unknown as typeof globalThis.ResizeObserver;
