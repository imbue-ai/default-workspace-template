// @vitest-environment jsdom
/**
 * A render smoke test over every body branch of the provider chooser.
 *
 * The known failure in this exact file was a render-time mithril throw -- a fragment mixing
 * keyed and unkeyed vnodes -- which aborted the redraw, left the spinner from the previous
 * frame on screen, and logged NOTHING on the server. It looked like a slow request for as
 * long as anyone cared to wait. No build error, no lint error, no type error.
 *
 * So the assertions here are mostly "this renders at all". That is the bug class; anything
 * fancier would be testing the mockup's copy rather than the failure mode.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import type { Lane, ProviderAccount } from "../models/Providers";

const state: {
  lanes: Lane[];
  accounts: ProviderAccount[];
  loaded: boolean;
  flow: unknown;
} = { lanes: [], accounts: [], loaded: true, flow: null };

vi.mock("../models/Providers", () => ({
  getLanes: () => state.lanes,
  getAccounts: () => state.accounts,
  areLanesLoaded: () => state.loaded,
  getFlow: () => state.flow,
  loadLanes: async () => undefined,
  loadAccounts: async () => undefined,
  deleteAccount: async () => undefined,
  startFlow: async () => undefined,
  submitCode: async () => undefined,
  submitKey: async () => undefined,
  abortFlow: () => undefined,
  clearFlow: () => undefined,
  isProviderChooserOpen: () => true,
  closeProviderChooser: () => undefined,
}));

import m from "mithril";

import { ProviderChooserModal } from "./ProviderChooserModal";

/** Render into a real element, not just call `view()`.
 *
 * This runs under jsdom on purpose. Mithril validates the keyed/unkeyed rule during its DOM
 * DIFF, not while building vnodes -- so walking the tree `view()` returns cannot see the very
 * crash this file exists for. */
function render(): string {
  const root = document.createElement("div");
  m.render(root, m(ProviderChooserModal as never, { onClose: () => undefined }));
  return root.textContent ?? "";
}

function lane(overrides: Partial<Lane> = {}): Lane {
  return {
    id: "anthropic",
    provider_name: "Anthropic",
    subtitle: "",
    harness: "claude",
    methods: [
      {
        id: "subscription",
        label: "Claude subscription",
        description: "Sign in with your Claude account.",
        signup_url: "",
        shape: "url_then_code",
        is_primary: true,
      },
    ],
    key_providers: [],
    ...overrides,
  } as Lane;
}

const PI_KEY_LANE = lane({
  id: "api-key",
  provider_name: "API key",
  harness: "pi-coding",
  methods: [
    {
      id: "api_key",
      label: "Paste a key",
      description: "Pick the provider, then paste its key.",
      signup_url: "",
      shape: "paste",
      is_primary: true,
    },
  ],
  key_providers: [
    { provider_id: "groq", display: "Groq", env_var: "GROQ_API_KEY", hint: "gsk-..." },
    { provider_id: "openrouter", display: "OpenRouter", env_var: "OPENROUTER_API_KEY", hint: "sk-or-..." },
  ],
});

beforeEach(() => {
  state.lanes = [lane()];
  state.accounts = [];
  state.loaded = true;
  state.flow = null;
});

describe("the provider chooser", () => {
  it("renders the lane list", () => {
    expect(render()).toContain("Anthropic");
  });

  it("renders a spinner before the lanes arrive", () => {
    state.loaded = false;
    expect(render()).toContain("Loading providers");
  });

  it("renders the signed-in accounts beside the lanes", () => {
    state.accounts = [
      {
        id: "a1",
        lane: "anthropic",
        harness: "claude",
        provider: "Anthropic",
        harness_label: "Claude Code",
        seq: 1,
        name: "",
        label: "Anthropic (Claude Code)",
      },
      {
        id: "a2",
        lane: "anthropic",
        harness: "claude",
        // Second of a duplicate pair: the number rides the provider noun, which is the span
        // the row actually draws -- see `numbered_provider`.
        provider: "Anthropic 2",
        harness_label: "Claude Code",
        seq: 2,
        name: "",
        label: "Anthropic 2 (Claude Code)",
      },
    ];
    const text = render();
    expect(text).toContain("Signed in");
    expect(text).toContain("Anthropic 2 (Claude Code)");
  });

  it("renders a lane whose key picker has several providers", () => {
    // The prior crash was here: a keyed option list with one unkeyed placeholder in it.
    state.lanes = [PI_KEY_LANE];
    expect(() => render()).not.toThrow();
  });

  it("renders every lane in one list without throwing", () => {
    state.lanes = [lane(), PI_KEY_LANE, lane({ id: "google", provider_name: "Google", harness: "antigravity" })];
    const text = render();
    expect(text).toContain("Anthropic");
    expect(text).toContain("Google");
    expect(text).toContain("API key");
  });

  it("renders a live flow's spinner", () => {
    state.flow = {
      flow_id: "f1",
      shape: "url_then_code",
      status: { state: "pending", detail: null, account_id: null },
    };
    expect(() => render()).not.toThrow();
  });

  it("renders a failed flow's error", () => {
    state.flow = {
      flow_id: "f1",
      shape: "url_then_code",
      status: { state: "failed", detail: "That code did not work.", account_id: null },
    };
    expect(() => render()).not.toThrow();
  });

  it("renders a finished flow", () => {
    state.flow = {
      flow_id: "f1",
      shape: "paste",
      status: { state: "ok", detail: null, account_id: "a1" },
    };
    expect(() => render()).not.toThrow();
  });
});
