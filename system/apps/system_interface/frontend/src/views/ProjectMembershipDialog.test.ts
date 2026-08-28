import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws; vitest's node environment has none (see ProjectSettingsModal.test).
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import type { ProjectInfo } from "../models/Projects";
import { ProjectMembershipDialog } from "./ProjectMembershipDialog";
import type { ProjectMembershipDialogAttrs } from "./ProjectMembershipDialog";

function project(id: string, name: string): ProjectInfo {
  return { project_id: id, name, color: "#4f8ef7", glyph: 1, has_content: true, members: [] };
}

const PROJECTS = [project("alpha", "Alpha"), project("beta", "Beta"), project("gamma", "Gamma")];

type VnodeLike = {
  attrs?: Record<string, unknown>;
  children?: unknown;
  tag?: unknown;
};

/** Depth-first walk over a rendered Mithril vnode tree (the shared shape of
 *  every unmounted-modal test in this directory). */
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
  }
}

function checkboxes(tree: unknown): VnodeLike[] {
  return [...walk(tree)].filter((vnode) => vnode.tag === "input" && vnode.attrs?.type === "checkbox");
}

function checkboxByLabel(tree: unknown, label: string): VnodeLike | undefined {
  return checkboxes(tree).find((vnode) => vnode.attrs?.["aria-label"] === label);
}

// The confirm ("Add") button rides the shared Modal's `actions` prop, so it
// is reached through the root Modal vnode's attrs rather than its children.
function confirmButton(tree: unknown): VnodeLike | undefined {
  const actions = (tree as VnodeLike).attrs?.actions;
  for (const vnode of walk(actions)) {
    const classes = vnode.attrs?.className;
    if (typeof classes === "string" && classes.split(/\s+/).includes("btn--primary")) {
      return vnode;
    }
  }
  return undefined;
}

function setChecked(vnode: VnodeLike, checked: boolean): void {
  (vnode.attrs?.onchange as (e: { target: { checked: boolean } }) => void)({ target: { checked } });
}

function makeDialog(attrs: ProjectMembershipDialogAttrs): { render: () => unknown } {
  const component = ProjectMembershipDialog();
  const vnode = { attrs };
  if (component.oninit) component.oninit(vnode as Parameters<NonNullable<typeof component.oninit>>[0]);
  return {
    render: () => component.view(vnode as Parameters<typeof component.view>[0]),
  };
}

function makeAttrs(overrides: Partial<ProjectMembershipDialogAttrs> = {}): ProjectMembershipDialogAttrs {
  return {
    memberLabel: "Chat 1",
    projects: PROJECTS,
    memberProjectIds: ["alpha"],
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    ...overrides,
  };
}

describe("ProjectMembershipDialog", () => {
  it("fixes the projects already showing the object and starts with nothing else checked", () => {
    const dialog = makeDialog(makeAttrs());
    const tree = dialog.render();
    const alpha = checkboxByLabel(tree, "Alpha");
    expect(alpha?.attrs?.checked).toBe(true);
    expect(alpha?.attrs?.disabled).toBe(true);
    const beta = checkboxByLabel(tree, "Beta");
    expect(beta?.attrs?.checked).toBe(false);
    expect(beta?.attrs?.disabled).toBe(false);
  });

  it("enables the confirm only once a new project is checked, then reports the checked set", () => {
    const attrs = makeAttrs();
    const dialog = makeDialog(attrs);
    expect(confirmButton(dialog.render())?.attrs?.disabled).toBe(true);

    setChecked(checkboxByLabel(dialog.render(), "Beta")!, true);
    setChecked(checkboxByLabel(dialog.render(), "Gamma")!, true);
    setChecked(checkboxByLabel(dialog.render(), "Gamma")!, false);
    const confirm = confirmButton(dialog.render());
    expect(confirm?.attrs?.disabled).toBe(false);
    (confirm?.attrs?.onclick as () => void)();
    expect(attrs.onConfirm).toHaveBeenCalledWith(["beta"]);
  });

  it("delegates backdrop dismissal to the shared modal shell, wired to onCancel", () => {
    // The dialog renders the shared Modal, handing it onCancel as the
    // dismissal handler. The mousedown-not-click, primary-button-only
    // behaviour lives in modalBackdrop.ts (covered by modalBackdrop.test.ts),
    // so here we only assert the wiring.
    const attrs = makeAttrs();
    const tree = makeDialog(attrs).render() as VnodeLike;
    expect(tree.attrs?.onclick).toBeUndefined();
    expect(tree.attrs?.onDismiss).toBe(attrs.onCancel);
  });
});
