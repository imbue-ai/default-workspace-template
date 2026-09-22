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
 * fancier would be testing the dialog's copy rather than the failure mode.
 *
 * The exception is picking a signed-in account, which hands the chosen account to the caller
 * and closes the chooser; those tests assert that behavior against the real chooser state.
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

const startFlow = vi.hoisted(() => vi.fn(async () => undefined));

// The real module under the view's data sources, so the chooser's open/pick/close state is the
// production code rather than a stand-in for it.
vi.mock("../models/Providers", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Providers")>()),
  getLanes: () => state.lanes,
  getAccounts: () => state.accounts,
  areLanesLoaded: () => state.loaded,
  getFlow: () => state.flow,
  loadLanes: async () => undefined,
  loadAccounts: async () => undefined,
  deleteAccount: async () => undefined,
  startFlow,
  submitCode: async () => undefined,
  submitKey: async () => undefined,
  abortFlow: () => undefined,
  clearFlow: () => undefined,
}));

import m from "mithril";

import { closeProviderChooser, isProviderChooserOpen, openProviderChooser } from "../models/Providers";
import type { UnpickableReason } from "../models/Providers";
import { ProviderChooserModal } from "./ProviderChooserModal";
import { ACCOUNT_FAILING_NOTE, ACCOUNT_NEUTRAL_NOTE } from "./providerSignInStyles";

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

function account(id: string, laneId: string, label: string): ProviderAccount {
  return { id, lane: laneId, harness: "claude", provider: label, harness_label: "", seq: 1, name: "", label };
}

beforeEach(() => {
  closeProviderChooser();
  startFlow.mockClear();
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
    // Signed-in leads; the lane list follows under its own header. What you already have
    // must not sit below the fold of a long provider list.
    expect(text.indexOf("Signed in")).toBeLessThan(text.indexOf("Add more"));
    expect(text.indexOf("Anthropic 2 (Claude Code)")).toBeLessThan(text.indexOf("Add more"));
  });

  it("shows neither section header when nothing is signed in", () => {
    const text = render();
    expect(text).toContain("Anthropic");
    expect(text).not.toContain("Signed in");
    expect(text).not.toContain("Add more");
  });

  it("asks for confirmation in a layered dialog before removing an account", () => {
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
    ];
    const root = document.createElement("div");
    const draw = () => m.render(root, m(ProviderChooserModal as never, { onClose: () => undefined }));
    draw();
    expect(root.textContent).not.toContain("Remove account");

    (root.querySelector('[aria-label="Remove Anthropic (Claude Code)"]') as HTMLElement).click();
    draw();
    expect(root.textContent).toContain("Remove account");
    expect(root.textContent).toContain("New chats can't be started on it.");
    expect(root.querySelector(".destroy-dialog-btn-destroy")?.textContent).toBe("Remove");

    (root.querySelector(".destroy-dialog-btn-cancel") as HTMLElement).click();
    draw();
    expect(root.textContent).not.toContain("Remove account");
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

describe("picking a signed-in account", () => {
  const ANTHROPIC = account("a1", "anthropic", "Anthropic (Claude Code)");
  const OPENAI = account("a2", "openai", "OpenAI (Pi)");

  function mount(): { root: HTMLElement; draw: () => void } {
    const root = document.createElement("div");
    const draw = (): void => m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));
    draw();
    return { root, draw };
  }

  function pickTarget(root: HTMLElement, accountId: string): HTMLButtonElement | null {
    return root.querySelector(`[data-e2e="pick-account-${accountId}"]`);
  }

  beforeEach(() => {
    state.accounts = [ANTHROPIC, OPENAI];
  });

  it("hands a working account to the caller and closes the chooser", () => {
    const onSignedIn = vi.fn();
    openProviderChooser({ onSignedIn, unpickable: { accountId: OPENAI.id, reason: "failing" } });
    const { root } = mount();

    pickTarget(root, ANTHROPIC.id)!.click();

    expect(onSignedIn).toHaveBeenCalledExactlyOnceWith(ANTHROPIC.id);
    expect(isProviderChooserOpen()).toBe(false);
    expect(startFlow).not.toHaveBeenCalled();
  });

  it("lists the failing account without letting it be picked, keeping its actions", () => {
    const onSignedIn = vi.fn();
    openProviderChooser({ onSignedIn, unpickable: { accountId: OPENAI.id, reason: "failing" } });
    const { root, draw } = mount();

    const broken = pickTarget(root, OPENAI.id)!;
    expect(broken.disabled).toBe(true);
    expect(broken.closest("div")!.textContent).toContain("Not working");
    broken.click();
    expect(onSignedIn).not.toHaveBeenCalled();
    expect(isProviderChooserOpen()).toBe(true);

    (root.querySelector('[aria-label="Remove OpenAI (Pi)"]') as HTMLElement).click();
    draw();
    expect(root.textContent).toContain("Remove account");
  });

  it("reads the note as an error only for an account the caller is leaving because it failed", () => {
    // An account is refused for two unrelated reasons -- it just failed, or the chat already runs
    // on it -- and only the first is bad news, so the two must not look alike.
    function noteClassFor(reason: UnpickableReason, note: string): string {
      openProviderChooser({ onSignedIn: vi.fn(), unpickable: { accountId: OPENAI.id, reason } });
      const { root } = mount();
      const row = pickTarget(root, OPENAI.id)!.closest("div")!;
      const rendered = [...row.querySelectorAll("span")].find((span) => span.textContent === note)!;
      closeProviderChooser();
      return rendered.className;
    }

    expect(noteClassFor("failing", "Not working")).toBe(ACCOUNT_FAILING_NOTE);
    expect(noteClassFor("current", "Current")).toBe(ACCOUNT_NEUTRAL_NOTE);
  });

  it("re-authenticates rather than picks when Sign in again is pressed on a pickable row", () => {
    const onSignedIn = vi.fn();
    openProviderChooser({ onSignedIn });
    const { root } = mount();

    const row = pickTarget(root, ANTHROPIC.id)!.parentElement!;
    const signInAgain = [...row.querySelectorAll("button")].find((b) => b.textContent === "Sign in again")!;
    signInAgain.click();

    expect(startFlow).toHaveBeenCalledExactlyOnceWith("anthropic", "subscription", ANTHROPIC.id);
    expect(onSignedIn).not.toHaveBeenCalled();
  });

  it("keeps signed-in rows as plain listings when opened only to add a provider", () => {
    openProviderChooser();
    const { root } = mount();

    expect(root.textContent).toContain("Anthropic (Claude Code)");
    expect(root.querySelector('[data-e2e^="pick-account-"]')).toBeNull();
  });
});
