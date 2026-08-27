/**
 * A render smoke test over every branch of the CURRENT model bar.
 *
 * Written deliberately against today's component, before the combo card replaces it, so
 * the rewrite has a before/after it can diff instead of a blank page. These assertions are
 * about what the user can see and click -- not about internals -- so they should survive a
 * rewrite that keeps the behaviour and fail loudly on one that does not.
 *
 * The known failure mode in this app's mithril views is a render-time throw: it aborts the
 * redraw, leaves the last painted frame up, and logs nothing on the server. Only a render
 * test catches that, which is why every branch below is exercised for real rather than
 * asserted about in the abstract.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

const agentState: { agent: unknown } = { agent: null };
vi.mock("../models/AgentManager", () => ({
  getAgentById: () => agentState.agent,
}));

const catalogState: { catalog: unknown } = { catalog: null };
vi.mock("../models/HarnessCatalog", () => ({
  ensureHarnessCatalogs: () => undefined,
  // Mirrors the real one: no harness, no catalog. An agent that is gone has no harness.
  getHarnessCatalog: (harness?: string) => (harness === undefined ? null : catalogState.catalog),
}));

const settingsState: { choice: unknown } = { choice: null };
vi.mock("../models/ModelSettings", () => ({
  effectiveChoice: () => settingsState.choice,
  changedAxes: () => [],
  setModelChoice: () => undefined,
}));

import { ModelBar } from "./ModelBar";

interface VnodeLike {
  tag?: unknown;
  attrs?: Record<string, unknown> | null;
  children?: unknown;
}

/** Depth-first walk of a rendered vnode tree, the way NewTabLauncher.test.ts does it: the
 *  default vitest environment has no DOM, and this app's views render fine without one. */
function flatten(node: unknown, out: unknown[] = []): unknown[] {
  if (node === null || node === undefined || typeof node === "boolean") return out;
  if (Array.isArray(node)) {
    for (const child of node) flatten(child, out);
    return out;
  }
  if (typeof node === "object" && "tag" in (node as VnodeLike)) {
    out.push(node);
    flatten((node as VnodeLike).children, out);
    return out;
  }
  out.push(node);
  return out;
}

/** Render the component's view and return what the user would see and could click. */
function render(): { text: string; tags: string[]; classes: string[]; tooltips: string[]; empty: boolean } {
  const tree = ModelBar().view({ attrs: { agentId: "a1" } } as never);
  const nodes = flatten(tree);
  const vnodes = nodes.filter((each): each is VnodeLike => typeof each === "object" && each !== null && "tag" in each);
  return {
    text: nodes.filter((each): each is string => typeof each === "string").join(" "),
    tags: vnodes.map((each) => String(each.tag)),
    // Mithril puts a rendered vnode's classes on `className`, not `class`.
    classes: vnodes.map((each) => String(each.attrs?.className ?? each.attrs?.class ?? "")),
    tooltips: vnodes.map((each) => String(each.attrs?.["data-tooltip"] ?? "")),
    empty: tree === null || tree === undefined,
  };
}

const OPUS = {
  id: "opus",
  label: "Opus",
  efforts: [],
  supports_fast: false,
  in_picker: true,
  harness_reported_model_id: null,
};
const SONNET = {
  id: "sonnet",
  label: "Sonnet",
  efforts: [],
  supports_fast: false,
  in_picker: true,
  harness_reported_model_id: null,
};

function catalogOf(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    switch_mode: "eager_then_reconcile",
    picker_mode: "list",
    options: [OPUS, SONNET],
    native_atomic_shoulder_tap_possible: true,
    popups: [],
    ...overrides,
  };
}

beforeEach(() => {
  agentState.agent = { id: "a1", harness: "claude" };
  catalogState.catalog = catalogOf();
  settingsState.choice = { identity: { model_id: "opus", effort: null, fast: false }, matched: OPUS, pending: null };
});

describe("the model bar", () => {
  it("renders nothing when the agent is unknown", () => {
    agentState.agent = null;
    expect(render().empty).toBe(true);
  });

  it("renders nothing when the harness has no catalog yet", () => {
    catalogState.catalog = null;
    expect(render().empty).toBe(true);
  });

  it("shows the matched model", () => {
    expect(render().text).toContain("Opus");
  });

  it("shrugs when the live model matches no catalog option", () => {
    // The codex case: an agent whose daemon has not answered `model/list` yet, or one that
    // is signed out. Both leave the bar with nothing to match against.
    settingsState.choice = {
      identity: { model_id: "gpt-9", effort: null, fast: false },
      matched: null,
      pending: null,
    };
    const { text } = render();
    expect(text).toContain("\u{1F937}");
    expect(text).not.toContain("Opus");
  });

  it("renders a read-only harness's model as a statement, not a control", () => {
    // agy: its `/model` is an interactive TUI with no scriptable form, so a picker here would
    // offer a switch that cannot work. It stays a <button> deliberately -- a disabled one
    // suppresses :hover and would kill the tooltip that explains why -- so the invariants are
    // the readonly class, no chevron, and the explaining tooltip.
    catalogState.catalog = catalogOf({ switch_mode: "read_only" });
    const { text, classes, tooltips } = render();
    expect(text).toContain("Opus");
    expect(classes.some((each) => each.includes("--readonly"))).toBe(true);
    expect(classes.some((each) => each.includes("model-selector-chevron"))).toBe(false);
    expect(tooltips.join(" ").toLowerCase()).toContain("terminal");
  });

  it("renders an effort slot only when the matched model declares efforts", () => {
    expect(render().text).not.toContain("Medium");
    const withEffort = { ...OPUS, efforts: [{ level: "medium", label: "Medium", in_picker: true }] };
    catalogState.catalog = catalogOf({ options: [withEffort] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "medium", fast: false },
      matched: withEffort,
      pending: null,
    };
    expect(render().text).toContain("Medium");
  });

  it("renders a fast slot only when the matched model supports it", () => {
    expect(render().classes.some((each) => each.includes("fast-toggle"))).toBe(false);
    const fastModel = { ...OPUS, supports_fast: true };
    catalogState.catalog = catalogOf({ options: [fastModel] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: null, fast: true },
      matched: fastModel,
      pending: null,
    };
    const { classes } = render();
    expect(classes.some((each) => each.includes("fast-toggle--on"))).toBe(true);
  });

  it("survives a harness whose options are populated instead of its models", () => {
    // codex is a "dynamic" picker: its static options are EMPTY by design, because its model
    // set is per-account and comes from its own daemon. An empty catalog must not throw.
    catalogState.catalog = catalogOf({ picker_mode: "dynamic", switch_mode: "on_change", options: [] });
    settingsState.choice = {
      identity: { model_id: "gpt-5", effort: null, fast: false },
      matched: null,
      pending: null,
    };
    expect(() => render()).not.toThrow();
  });
});
