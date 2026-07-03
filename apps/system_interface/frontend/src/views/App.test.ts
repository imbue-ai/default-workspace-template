import { afterEach, describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, which
// makes the `m.redraw()` calls inside the model setters throw. Provide a
// polyfill before any import is evaluated (same setup as
// ClaudeLoginModal.test.ts / GitHubLoginModal.test.ts).
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import { App } from "./App";
import { InspirationPublishModal } from "./InspirationPublishModal";
import { openInspirationModal, closeInspirationModal } from "../models/InspirationPublish";
import type { InspirationProposal } from "../models/InspirationPublish";

const PROPOSAL: InspirationProposal = {
  slug: "test-inspiration",
  title: "Test Inspiration",
  description: "A test proposal",
  repo_name: "test-inspiration",
  visibility: "private",
  thumbnail_svg: "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
};

type VnodeLike = {
  tag?: unknown;
  children?: unknown;
};

// App's view() ignores its vnode argument; mithril's Component type still
// requires one, so pass a minimal stand-in.
function renderApp(): unknown {
  const component = App();
  return component.view({} as Parameters<typeof component.view>[0]);
}

// Depth-first walk over a rendered Mithril vnode tree.
function* walk(node: unknown): Generator<VnodeLike> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (node !== null && typeof node === "object") {
    const vnode = node as VnodeLike;
    yield vnode;
    if (vnode.children !== undefined) yield* walk(vnode.children);
  }
}

afterEach(() => {
  // Unconditional close: null slug matches whatever proposal is open.
  closeInspirationModal(null);
});

describe("App", () => {
  it("renders its view without a proposal", () => {
    expect(() => renderApp()).not.toThrow();
  });

  it("renders the keyed inspiration modal without breaking the redraw", () => {
    // Regression test: the modal vnode is keyed by slug, and mithril requires
    // every children array to be all-keyed or all-unkeyed. Placing the keyed
    // vnode directly in App's unkeyed children array made this view() call
    // throw ("In fragments, vnodes must either all have keys or none have
    // keys") on the redraw that followed inspiration_publish_requested, so
    // the publish popup never rendered. The keyed vnode must live in its own
    // single-child fragment.
    openInspirationModal(PROPOSAL);
    const tree = renderApp();
    const hasModal = [...walk(tree)].some((vnode) => vnode.tag === InspirationPublishModal);
    expect(hasModal).toBe(true);
  });
});
