import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, which
// makes the `m.redraw()` calls inside the modal's event handlers throw.
// Provide a polyfill before any import is evaluated, as other modal tests in
// this directory do for the same reason.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// apiUrl reads the base path from a <meta> tag, which vitest's node
// environment has no document for; identity keeps the asserted URLs the bare
// /api paths (mirrors Projects.test.ts).
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import type { ProjectInfo } from "../models/Projects";
import { ProjectSettingsModal } from "./ProjectSettingsModal";
import type { ProjectSettingsModalAttrs } from "./ProjectSettingsModal";

// Members are carried but never read: the modal is display metadata (name,
// color, glyph) plus the delete, and taking an object out of a project is a
// verb on the object's own rail row rather than anything reachable from here.
const PROJECT: ProjectInfo = {
  project_id: "research",
  name: "Research",
  color: "#4f8ef7",
  glyph: 3,
  has_content: true,
  members: [],
};

type VnodeLike = {
  attrs?: Record<string, unknown>;
  children?: unknown;
  tag?: unknown;
};

/** Depth-first walk over a rendered Mithril vnode tree (the helper every
 *  modal test in this directory that inspects a tree without mounting it
 *  re-implements). The modal's footer buttons ride the shared Modal shell's
 *  `actions` prop rather than its children, so the walk descends into that
 *  slot too. */
function* walk(node: unknown): Generator<VnodeLike> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (node !== null && typeof node === "object") {
    const vnode = node as VnodeLike;
    // A closure-component vnode (e.g. m(Button, ...)) carries no markup of its
    // own -- its view runs only when mithril renders it -- so expand it and
    // walk what it renders.
    if (typeof vnode.tag === "function") {
      const component = (vnode.tag as (v: VnodeLike) => { view: (v: VnodeLike) => unknown })(vnode);
      yield* walk(component.view(vnode));
      return;
    }
    yield vnode;
    if (vnode.children !== undefined) yield* walk(vnode.children);
    if (vnode.attrs?.actions !== undefined) yield* walk(vnode.attrs.actions);
  }
}

function findByClass(tree: unknown, className: string): VnodeLike | undefined {
  for (const vnode of walk(tree)) {
    const classes = vnode.attrs?.className;
    if (typeof classes === "string" && classes.split(/\s+/).includes(className)) {
      return vnode;
    }
  }
  return undefined;
}

function clickVnode(vnode: VnodeLike): void {
  (vnode.attrs?.onclick as () => void)();
}

function makeModal(attrs: ProjectSettingsModalAttrs): { render: () => unknown } {
  const component = ProjectSettingsModal();
  const vnode = { attrs };
  if (component.oninit) component.oninit(vnode as Parameters<NonNullable<typeof component.oninit>>[0]);
  return {
    render: () => component.view(vnode as Parameters<typeof component.view>[0]),
  };
}

describe("ProjectSettingsModal delete confirmation", () => {
  it("describes deleting as removing the view only, with members left running", () => {
    const modal = makeModal({
      project: PROJECT,
      onSaved: () => {},
      onDeleted: () => {},
      onCancel: () => {},
    });
    // The "Delete" trigger (reveals the confirmation) is the only secondary button
    // in the initial render.
    const deleteButton = findByClass(modal.render(), "btn--secondary");
    expect(deleteButton).toBeDefined();
    clickVnode(deleteButton!);

    const tree = JSON.stringify(modal.render());
    expect(tree).toContain("removes the view only");
    expect(tree).toContain("keeps running");
    expect(tree).toContain("Everything");
    // The old, now-inaccurate warning about stopping terminals and browsers is gone.
    expect(tree).not.toContain("shut down");
  });
});
