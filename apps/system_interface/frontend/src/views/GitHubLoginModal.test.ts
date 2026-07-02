import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, which
// makes the `m.redraw()` calls inside the modal's event handlers throw.
// Provide a polyfill before any import is evaluated so the handlers can be
// exercised in tests (same setup as ClaudeLoginModal.test.ts).
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import { GitHubLoginModal } from "./GitHubLoginModal";

type VnodeLike = {
  attrs?: Record<string, unknown>;
  children?: unknown;
};

function makeModal(): { render: () => unknown } {
  const component = GitHubLoginModal();
  const vnode = { attrs: { onDismiss: () => {} } };
  return {
    render: () => component.view(vnode as Parameters<typeof component.view>[0]),
  };
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

// Find a `<button>` vnode whose rendered text contains `text` and invoke its
// onclick. Matches on the vnode tag (the hyperscript selector) so it picks the
// button itself rather than an ancestor container that merely contains the text.
function clickButtonByText(tree: unknown, text: string): void {
  for (const vnode of walk(tree)) {
    const onclick = vnode.attrs?.onclick;
    const tag = (vnode as { tag?: unknown }).tag;
    if (
      typeof onclick === "function" &&
      typeof tag === "string" &&
      tag.startsWith("button") &&
      JSON.stringify(vnode.children ?? "").includes(text)
    ) {
      (onclick as () => void)();
      return;
    }
  }
  throw new Error(`No button found with text: ${text}`);
}

// Let queued microtasks + the setTimeout-based redraw polyfill drain.
const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

describe("GitHubLoginModal", () => {
  it("renders the default method-selection view without throwing", () => {
    expect(() => makeModal().render()).not.toThrow();
    const tree = JSON.stringify(makeModal().render());
    expect(tree).toContain("Continue with GitHub");
  });

  it("surfaces the real rejection reason (not the generic fallback) when the start request fails", async () => {
    // The node test env has no XHR/`FormData` global, so `m.request` rejects
    // with a real `TypeError` (no parseable JSON body) -- the same shape as a
    // network-level failure in the real app (e.g. a proxy error the backend
    // never saw). Before the fix, this modal's inline error extraction only
    // ever read `error.response.detail` and fell back to the hardcoded
    // "Failed to start GitHub login" whenever that was absent, discarding the
    // real `TypeError` message. It now goes through the shared
    // `describeRequestError` helper (already used by InspirationPublishModal
    // and ClaudeLoginModal's peers), which also falls back to `error.message`
    // before giving up -- so the actual reason is shown here instead.
    const modal = makeModal();
    clickButtonByText(modal.render(), "Continue with GitHub");
    await flush();

    const failed = modal.render();
    const serialized = JSON.stringify(failed);
    expect(serialized).toContain("Something went wrong");
    expect(serialized).toContain("Start over");
    // The old hardcoded fallback is gone in favor of the real TypeError
    // message that `describeRequestError` extracted via its `message` tier
    // (the exact wording depends on which DOM global Mithril's request path
    // trips over first in this no-XHR test env, e.g. "document is not
    // defined" -- what matters is that it is NOT the old opaque constant).
    expect(serialized).not.toContain("Failed to start GitHub login");
    expect(serialized).toContain("is not defined");
  });

  it("Start over returns to method selection with a clean error state", async () => {
    const modal = makeModal();
    clickButtonByText(modal.render(), "Continue with GitHub");
    await flush();

    const failed = modal.render();
    expect(JSON.stringify(failed)).toContain("Start over");

    clickButtonByText(failed, "Start over");
    await flush();

    const restarted = modal.render();
    const serialized = JSON.stringify(restarted);
    expect(serialized).toContain("Continue with GitHub");
    // The prior failure's error text must not linger into the fresh attempt.
    expect(serialized).not.toContain("Something went wrong");
  });
});
