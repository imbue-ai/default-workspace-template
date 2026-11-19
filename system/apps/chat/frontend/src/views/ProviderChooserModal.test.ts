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

const startFlow = vi.hoisted(() => vi.fn(async (..._args: unknown[]) => undefined));
const requestProviderRelay = vi.hoisted(() => vi.fn(async (..._args: unknown[]) => true));
const loadLanesAndAccountsWithRetry = vi.hoisted(() => vi.fn(async () => undefined));
const findLocalModelServers = vi.hoisted(() => vi.fn(async (..._args: unknown[]): Promise<unknown> => undefined));
const submitEndpoint = vi.hoisted(() => vi.fn(async (..._args: unknown[]) => undefined));

vi.mock("../models/providerRelay", () => ({ requestProviderRelay }));

// The real module under the view's data sources, so the chooser's open/pick/close state is the
// production code rather than a stand-in for it.
vi.mock("../models/Providers", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Providers")>()),
  getLanes: () => state.lanes,
  getAccounts: () => state.accounts,
  areLanesLoaded: () => state.loaded,
  getFlow: () => state.flow,
  loadLanesAndAccountsWithRetry,
  deleteAccount: async () => undefined,
  startFlow,
  submitCode: async () => undefined,
  submitKey: async () => undefined,
  submitEndpoint,
  findLocalModelServers,
  abortFlow: () => undefined,
  clearFlow: () => undefined,
}));

import m from "mithril";

import { closeProviderChooser, isProviderChooserOpen, openProviderChooser } from "../models/Providers";
import type { UnpickableReason } from "../models/Providers";
import { PASTE_STEP_LABEL, ProviderChooserModal } from "./ProviderChooserModal";
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
        note: "",
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
      note: "",
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
  return {
    id,
    lane: laneId,
    harness: "claude",
    provider: label,
    harness_label: "",
    seq: 1,
    name: "",
    label,
    reauth_method: "subscription",
    signed_in_at: null,
  };
}

beforeEach(() => {
  closeProviderChooser();
  startFlow.mockClear();
  requestProviderRelay.mockReset();
  requestProviderRelay.mockResolvedValue(true);
  loadLanesAndAccountsWithRetry.mockReset();
  loadLanesAndAccountsWithRetry.mockResolvedValue(undefined);
  state.lanes = [lane()];
  state.accounts = [];
  state.loaded = true;
  state.flow = null;
});

describe("the provider chooser", () => {
  it("renders the lane list", () => {
    expect(render()).toContain("Anthropic");
  });

  it("marks its card, header, and title as the dialog parts a theme styles", () => {
    const root = document.createElement("div");
    m.render(root, m(ProviderChooserModal as never, { onClose: () => undefined }));
    const card = root.querySelector('[data-part="dialog"]');
    expect(card?.getAttribute("role")).toBe("dialog");
    expect(card?.querySelector('[data-part="dialog-header"] [data-part="dialog-title"]')?.textContent).not.toBe("");
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
        reauth_method: "subscription",
        signed_in_at: null,
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
        reauth_method: "subscription",
        signed_in_at: null,
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
        reauth_method: "subscription",
        signed_in_at: null,
      },
    ];
    const root = document.createElement("div");
    const draw = () => m.render(root, m(ProviderChooserModal as never, { onClose: () => undefined }));
    draw();
    expect(root.textContent).not.toContain("Remove account");

    (root.querySelector('[aria-label="Remove Anthropic (Claude Code)"]') as HTMLElement).click();
    draw();
    expect(root.textContent).toContain("Remove account");
    expect(root.textContent).toContain("chats running on it stop taking messages");
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

describe("a provider list that cannot be loaded", () => {
  async function settled(): Promise<void> {
    for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
  }

  it("offers Try again under the hook the e2e runner presses, and loads the list again", async () => {
    state.loaded = false;
    loadLanesAndAccountsWithRetry.mockRejectedValueOnce(new Error("Service Unavailable"));
    const root = document.createElement("div");
    const draw = (): void => m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));
    draw();
    await settled();
    draw();

    const retry = root.querySelector("[data-e2e=retry-providers]");
    expect(root.textContent).toContain("Could not load providers");
    expect(retry?.textContent).toBe("Try again");

    state.loaded = true;
    (retry as HTMLButtonElement).click();
    await settled();
    draw();

    expect(loadLanesAndAccountsWithRetry).toHaveBeenCalledTimes(2);
    expect(root.querySelector("[data-e2e=retry-providers]")).toBeNull();
    expect(root.querySelector("[data-e2e=lane-anthropic]")).not.toBeNull();
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

  it("offers a room under the providers when opened for a chat awaiting its first send, and hands it over", () => {
    const onRoomChosen = vi.fn();
    const onDismissed = vi.fn();
    openProviderChooser({ onRoomChosen, onDismissed });
    const { root } = mount();

    const text = root.textContent ?? "";
    expect(text.indexOf("Chat room (no provider)")).toBeGreaterThan(text.indexOf("Anthropic (Claude Code)"));
    root.querySelector<HTMLButtonElement>('[data-e2e="pick-room"]')!.click();

    expect(onRoomChosen).toHaveBeenCalledOnce();
    expect(onDismissed).not.toHaveBeenCalled();
    expect(isProviderChooserOpen()).toBe(false);
    expect(startFlow).not.toHaveBeenCalled();
  });

  it("offers no room when opened to pick or add a provider", () => {
    openProviderChooser({ onSignedIn: vi.fn() });
    expect(mount().root.querySelector('[data-e2e="pick-room"]')).toBeNull();
    closeProviderChooser();

    openProviderChooser();
    expect(mount().root.querySelector('[data-e2e="pick-room"]')).toBeNull();
  });
});

describe("a sign-in finished in the browser", () => {
  const CLAUDE_RELAY_URL = "https://claude.ai/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A54871%2Fcallback";
  const CHATGPT = lane({
    id: "openai",
    provider_name: "OpenAI",
    harness: "codex",
    methods: [
      {
        id: "chatgpt",
        label: "Use your ChatGPT plan (runs on Codex)",
        description: "",
        signup_url: "",
        note: "",
        shape: "browser",
        is_primary: true,
      },
      {
        id: "device",
        label: "ChatGPT with a code",
        description: "",
        signup_url: "",
        note: "",
        shape: "code_then_wait",
        is_primary: false,
      },
    ],
  });

  /** The Anthropic lane with its API-key method offered under the browser sign-in. */
  function anthropicWithApiKey(): Lane {
    return lane({
      methods: [
        ...lane().methods,
        {
          id: "api_key",
          label: "Use an API key",
          description: "",
          signup_url: "",
          note: "",
          shape: "paste",
          is_primary: false,
        },
      ],
    });
  }

  async function settled(): Promise<void> {
    for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
  }

  async function clickLane(laneId: string): Promise<HTMLElement> {
    const root = document.createElement("div");
    const draw = (): void => m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));
    draw();
    (root.querySelector(`[data-e2e="lane-${laneId}"]`) as HTMLElement).click();
    await settled();
    draw();
    return root;
  }

  function startedFlow(shape: string, relayUrl: string | null): unknown {
    return {
      flow_id: "flow-1",
      shape,
      url: "https://claude.ai/manual",
      code: null,
      relay_url: relayUrl,
      laneId: "anthropic",
      methodId: "subscription",
      accountId: null,
      status: { state: "pending", detail: null, account_id: null },
    };
  }

  it("hands the page to the desktop app and waits on the browser", async () => {
    state.flow = startedFlow("url_then_code", CLAUDE_RELAY_URL);

    const root = await clickLane("anthropic");

    expect(requestProviderRelay).toHaveBeenCalledExactlyOnceWith(CLAUDE_RELAY_URL, "flow-1");
    expect(root.textContent).toContain("Finish signing in to Anthropic in your browser.");
    expect(root.querySelector('[data-e2e="open-sign-in-again"]')?.textContent).toBe("Try again");
  });

  it("offers reopening the page and another way in side by side while the browser is out", async () => {
    state.lanes = [anthropicWithApiKey()];
    state.flow = startedFlow("url_then_code", CLAUDE_RELAY_URL);

    const root = await clickLane("anthropic");

    expect(root.querySelector('[data-e2e="open-sign-in-again"]')?.textContent).toBe("Try again");
    expect(root.querySelector('[data-e2e="sign-in-another-way"]')?.textContent).toBe("Try another way");
    expect(root.textContent).not.toContain(PASTE_STEP_LABEL);
  });

  it("shows Claude's paste-the-code steps when nothing relays", async () => {
    requestProviderRelay.mockResolvedValue(false);
    state.flow = startedFlow("url_then_code", CLAUDE_RELAY_URL);

    const root = await clickLane("anthropic");

    expect(root.textContent).toContain(PASTE_STEP_LABEL);
    expect(root.textContent).not.toContain("Finish signing in to Anthropic in your browser.");
  });

  it("falls back to ChatGPT's one-time code when nothing relays", async () => {
    requestProviderRelay.mockResolvedValue(false);
    state.lanes = [CHATGPT];
    state.flow = startedFlow("browser", "https://auth.openai.com/oauth/authorize?state=s");

    await clickLane("openai");

    expect(startFlow).toHaveBeenCalledWith("openai", "chatgpt", undefined);
    expect(startFlow).toHaveBeenLastCalledWith("openai", "device", undefined);
  });

  it("goes straight to ChatGPT's one-time code once nothing relays, rather than starting the browser login again", async () => {
    requestProviderRelay.mockResolvedValue(false);
    state.lanes = [CHATGPT];
    state.flow = startedFlow("browser", "https://auth.openai.com/oauth/authorize?state=s");
    const root = await clickLane("openai");

    (root.querySelector('[aria-label="Back"]') as HTMLElement).click();
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));
    (root.querySelector('[data-e2e="lane-openai"]') as HTMLElement).click();
    await settled();

    expect(startFlow.mock.calls.map((call) => call[1])).toEqual(["chatgpt", "device", "device"]);
  });

  it("starts no sign-in until a row is clicked, so opening the chooser never displaces one", async () => {
    openProviderChooser();
    const root = document.createElement("div");
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));

    await settled();

    expect(startFlow).not.toHaveBeenCalled();
  });

  it("keeps Claude's code steps one click from the browser wait, on the same sign-in", async () => {
    state.lanes = [anthropicWithApiKey()];
    state.flow = startedFlow("url_then_code", CLAUDE_RELAY_URL);
    const root = await clickLane("anthropic");

    (root.querySelector('[data-e2e="sign-in-another-way"]') as HTMLElement).click();
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));

    expect(root.textContent).toContain(PASTE_STEP_LABEL);
    expect(root.textContent).toContain("Use an API key");
    expect(startFlow).toHaveBeenCalledOnce();
  });

  const CHATGPT_WITH_NOTE = lane({
    ...CHATGPT,
    methods: CHATGPT.methods.map((each) =>
      each.id === "chatgpt"
        ? { ...each, note: "Turn on *the setting* in [the settings](https://example.com/settings)." }
        : each,
    ),
  });

  it("shows the browser sign-in's note while it waits on the browser", async () => {
    state.lanes = [CHATGPT_WITH_NOTE];
    state.flow = startedFlow("browser", "https://auth.openai.com/oauth/authorize?state=s");

    const root = await clickLane("openai");

    expect(root.textContent).toContain("Finish signing in to OpenAI in your browser.");
    const note = root.querySelector('[data-e2e="method-note"]');
    expect(note?.textContent?.trim()).toBe("Turn on the setting in the settings.");
    expect(note?.querySelector("em")?.textContent).toBe("the setting");
    expect(note?.querySelector("a")?.getAttribute("href")).toBe("https://example.com/settings");
  });

  it("shows no note on the one-time code, which carries none of its own", async () => {
    requestProviderRelay.mockResolvedValue(false);
    state.lanes = [CHATGPT_WITH_NOTE];
    state.flow = { ...(startedFlow("code_then_wait", null) as object), code: "ABCD-1234" };

    const root = await clickLane("openai");

    expect(root.textContent).toContain("ABCD-1234");
    expect(root.querySelector('[data-e2e="method-note"]')).toBeNull();
  });

  it("moves ChatGPT to its one-time code when the browser is not coming back", async () => {
    state.lanes = [CHATGPT];
    state.flow = startedFlow("browser", "https://auth.openai.com/oauth/authorize?state=s");
    const root = await clickLane("openai");

    (root.querySelector('[data-e2e="sign-in-another-way"]') as HTMLElement).click();
    await settled();

    expect(startFlow).toHaveBeenLastCalledWith("openai", "device", undefined);
  });

  it("moves to another way in when the desktop app can no longer open the page", async () => {
    state.flow = startedFlow("url_then_code", CLAUDE_RELAY_URL);
    const root = await clickLane("anthropic");
    requestProviderRelay.mockResolvedValue(false);

    (root.querySelector('[data-e2e="open-sign-in-again"]') as HTMLElement).click();
    await settled();
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));

    expect(root.textContent).toContain("Imbue Studio couldn't open the sign-in page.");
    expect(root.textContent).toContain(PASTE_STEP_LABEL);
  });

  it("starts nothing when the desktop app's late answer to Try again comes after the user went back", async () => {
    state.flow = startedFlow("url_then_code", CLAUDE_RELAY_URL);
    const root = await clickLane("anthropic");
    let answer: (isRelaying: boolean) => void = () => undefined;
    requestProviderRelay.mockImplementationOnce(() => new Promise<boolean>((resolve) => (answer = resolve)));

    (root.querySelector('[data-e2e="open-sign-in-again"]') as HTMLElement).click();
    (root.querySelector('[aria-label="Back"]') as HTMLElement).click();
    // What the real abortFlow does to the live flow.
    state.flow = null;
    answer(false);
    await settled();

    expect(startFlow).toHaveBeenCalledOnce();
  });

  it("keeps waiting on the browser when Try again is clicked twice and the first ask is cut short", async () => {
    state.lanes = [CHATGPT];
    state.flow = startedFlow("browser", "https://auth.openai.com/oauth/authorize?state=s");
    const root = await clickLane("openai");
    let cutShort: (isRelaying: boolean) => void = () => undefined;
    requestProviderRelay.mockImplementationOnce(() => new Promise<boolean>((resolve) => (cutShort = resolve)));

    const tryAgain = root.querySelector('[data-e2e="open-sign-in-again"]') as HTMLElement;
    tryAgain.click();
    tryAgain.click();
    // What the real requestProviderRelay does to an ask a newer one replaced.
    cutShort(false);
    await settled();
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));

    expect(startFlow).toHaveBeenCalledOnce();
    expect(root.textContent).toContain("Finish signing in to OpenAI in your browser.");
  });

  it("stops offering ChatGPT's browser sign-in once the desktop app can no longer open the page", async () => {
    state.lanes = [CHATGPT];
    state.flow = startedFlow("browser", "https://auth.openai.com/oauth/authorize?state=s");
    const root = await clickLane("openai");
    requestProviderRelay.mockResolvedValue(false);

    (root.querySelector('[data-e2e="open-sign-in-again"]') as HTMLElement).click();
    await settled();
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));

    expect(startFlow).toHaveBeenLastCalledWith("openai", "device", undefined);
    expect(root.textContent).not.toContain("Use your ChatGPT plan (runs on Codex)");
  });

  it("offers another way in after a sign-in fails", async () => {
    state.lanes = [CHATGPT];
    state.flow = startedFlow("browser", "https://auth.openai.com/oauth/authorize?state=s");
    const root = await clickLane("openai");
    state.flow = { ...(state.flow as object), status: { state: "failed", detail: "Denied.", account_id: null } };
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));

    (root.querySelector('[data-e2e="sign-in-another-way"]') as HTMLElement).click();
    await settled();

    expect(startFlow).toHaveBeenLastCalledWith("openai", "device", undefined);
  });

  it("offers Claude's code steps after a sign-in fails, rather than the browser again", async () => {
    state.lanes = [anthropicWithApiKey()];
    state.flow = startedFlow("url_then_code", CLAUDE_RELAY_URL);
    const root = await clickLane("anthropic");
    state.flow = { ...(state.flow as object), status: { state: "failed", detail: "Denied.", account_id: null } };
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));
    requestProviderRelay.mockClear();
    startFlow.mockImplementationOnce(async () => {
      state.flow = startedFlow("url_then_code", CLAUDE_RELAY_URL);
    });

    (root.querySelector('[data-e2e="sign-in-another-way"]') as HTMLElement).click();
    await settled();
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));

    expect(startFlow).toHaveBeenLastCalledWith("anthropic", "subscription", undefined);
    expect(requestProviderRelay).not.toHaveBeenCalled();
    expect(root.textContent).toContain(PASTE_STEP_LABEL);
  });

  it("signs an API-key account in again with a key, not a browser sign-in", () => {
    state.lanes = [anthropicWithApiKey()];
    state.accounts = [{ ...account("a1", "anthropic", "Anthropic (Claude Code)"), reauth_method: "api_key" }];
    const root = document.createElement("div");
    m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));

    [...root.querySelectorAll("button")].find((b) => b.textContent === "Sign in again")!.click();

    expect(startFlow).toHaveBeenCalledExactlyOnceWith("anthropic", "api_key", "a1");
  });
});

describe("a local model", () => {
  const LOCAL_MODEL = lane({
    id: "local-model",
    provider_name: "Local model",
    harness: "pi-coding",
    methods: [
      {
        id: "endpoint",
        label: "Connect a model server",
        description: "Pick a model from a server running on your computer, or name another server.",
        signup_url: "",
        note: "",
        shape: "endpoint",
        is_primary: true,
      },
    ],
  });
  const OLLAMA = {
    base_url: "http://host.docker.internal:11434/v1",
    name: "Ollama",
    models: ["qwen2.5:7b"],
    choices: [{ model_id: "qwen2.5:7b", detail: "", context_windows: [], unusable_reason: null }],
    studio_port: null,
  };
  // An Ollama as it describes its models: one that runs at three sizes, and one too small for a chat.
  const DESCRIBED_OLLAMA = {
    ...OLLAMA,
    models: ["llama3.3:latest", "llama3.3-64k:latest", "qwen2.5:7b"],
    choices: [
      { model_id: "llama3.3:latest", detail: "70.6B", context_windows: [40960, 65536, 131072], unusable_reason: null },
      {
        model_id: "qwen2.5:7b",
        detail: "7.6B",
        context_windows: [],
        unusable_reason: "It takes a context of 32,768 tokens at most, and a chat here needs 40,960.",
      },
    ],
  };
  // The same server as Imbue Studio lists it for a workspace that is not on the user's computer.
  const STUDIO_OLLAMA = { ...OLLAMA, base_url: "http://127.0.0.1:11434/v1", studio_port: 11434 };

  function found(servers: unknown[], studioLink = "UNAVAILABLE"): unknown {
    return { servers, studio_link: studioLink, default_context_window: 32768, min_context_window: 16384 };
  }

  async function settled(): Promise<void> {
    for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
  }

  /** Open the chooser on the local-model lane, returning its root and a way to draw it again. */
  async function openForm(): Promise<{ root: HTMLElement; draw: () => void }> {
    const root = document.createElement("div");
    const draw = (): void => m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));
    draw();
    (root.querySelector('[data-e2e="lane-local-model"]') as HTMLElement).click();
    await settled();
    draw();
    return { root, draw };
  }

  function press(root: HTMLElement, hook: string): void {
    (root.querySelector(`[data-e2e="${hook}"]`) as HTMLElement).click();
  }

  function type(root: HTMLElement, hook: string, value: string): void {
    const field = root.querySelector(`[data-e2e="${hook}"]`) as HTMLInputElement;
    field.value = value;
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }

  beforeEach(() => {
    state.lanes = [LOCAL_MODEL];
    state.accounts = [];
    state.loaded = true;
    state.flow = {
      flow_id: "flow-1",
      shape: "endpoint",
      url: null,
      code: null,
      relay_url: null,
      status: { state: "pending", detail: null, account_id: null },
    };
    startFlow.mockClear();
    findLocalModelServers.mockReset();
    submitEndpoint.mockClear();
  });

  it("looks for servers on opening, picks the only model found, and connects with it", async () => {
    findLocalModelServers.mockResolvedValue(found([OLLAMA]));

    const { root } = await openForm();

    expect(findLocalModelServers).toHaveBeenCalledExactlyOnceWith(null, "");
    expect(root.textContent).toContain("Connect a local model");
    expect(root.textContent).toContain("qwen2.5:7b");
    expect((root.querySelector('[data-e2e="local-model-context"]') as HTMLInputElement).value).toBe("32768");
    press(root, "connect-local-model");
    expect(submitEndpoint).toHaveBeenCalledExactlyOnceWith({
      base_url: "http://host.docker.internal:11434/v1",
      model_id: "qwen2.5:7b",
      context_window: 32768,
      api_key: "",
      studio_port: null,
    });
  });

  it("waits for the user's approval in Imbue Studio, then offers the models on their computer", async () => {
    vi.useFakeTimers();
    try {
      findLocalModelServers.mockResolvedValueOnce(found([], "AWAITING_APPROVAL"));
      findLocalModelServers.mockResolvedValueOnce(found([], "AWAITING_APPROVAL"));
      findLocalModelServers.mockResolvedValueOnce(found([STUDIO_OLLAMA], "CONNECTED"));
      const root = document.createElement("div");
      const draw = (): void => m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));
      draw();
      (root.querySelector('[data-e2e="lane-local-model"]') as HTMLElement).click();
      await vi.advanceTimersByTimeAsync(0);
      draw();

      expect(root.querySelector('[data-e2e="local-model-awaiting-approval"]')?.textContent).toContain(
        "Approve it there",
      );
      expect(root.querySelector('[data-e2e="local-model-address"]')).toBeNull();
      expect(findLocalModelServers).toHaveBeenCalledExactlyOnceWith(null, "");

      // The checks that follow do not ask the user again.
      await vi.advanceTimersByTimeAsync(2000);
      expect(findLocalModelServers).toHaveBeenLastCalledWith(null, "", "never");
      await vi.advanceTimersByTimeAsync(2000);
      draw();

      expect(findLocalModelServers).toHaveBeenCalledTimes(3);
      expect(root.querySelector('[data-e2e="local-model-awaiting-approval"]')).toBeNull();
      expect(root.textContent).toContain("qwen2.5:7b");
      press(root, "connect-local-model");
      expect(submitEndpoint).toHaveBeenCalledExactlyOnceWith({
        base_url: "http://127.0.0.1:11434/v1",
        model_id: "qwen2.5:7b",
        context_window: 32768,
        api_key: "",
        studio_port: 11434,
      });

      // Approved: nothing is left checking.
      await vi.advanceTimersByTimeAsync(10000);
      expect(findLocalModelServers).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("asks the user again on request while it waits for their approval", async () => {
    findLocalModelServers.mockResolvedValue(found([], "AWAITING_APPROVAL"));

    const { root } = await openForm();
    press(root, "local-model-ask-again");
    await settled();

    expect(findLocalModelServers).toHaveBeenCalledTimes(2);
    expect(findLocalModelServers).toHaveBeenLastCalledWith(null, "", "again");
    // Closing the form stops it checking.
    m.render(root, null);
  });

  it("keeps waiting for the user's approval after a named server fails to answer", async () => {
    vi.useFakeTimers();
    try {
      findLocalModelServers.mockResolvedValueOnce(found([], "AWAITING_APPROVAL"));
      findLocalModelServers.mockRejectedValueOnce({
        response: { detail: "No server answered at http://box.lan:9000/v1." },
      });
      findLocalModelServers.mockResolvedValueOnce(found([STUDIO_OLLAMA], "CONNECTED"));
      const root = document.createElement("div");
      const draw = (): void => m.render(root, m(ProviderChooserModal as never, { onDismiss: () => undefined }));
      draw();
      (root.querySelector('[data-e2e="lane-local-model"]') as HTMLElement).click();
      await vi.advanceTimersByTimeAsync(0);
      draw();
      press(root, "local-model-name-server");
      draw();
      type(root, "local-model-address", "box.lan:9000");
      draw();
      press(root, "local-model-find");
      await vi.advanceTimersByTimeAsync(0);
      draw();

      expect(findLocalModelServers).toHaveBeenLastCalledWith("box.lan:9000", "");
      expect(root.querySelector('[data-e2e="local-model-error"]')?.textContent).toBe(
        "No server answered at http://box.lan:9000/v1.",
      );
      expect(root.querySelector('[data-e2e="local-model-awaiting-approval"]')).not.toBeNull();

      await vi.advanceTimersByTimeAsync(2000);
      draw();

      expect(findLocalModelServers).toHaveBeenLastCalledWith(null, "", "never");
      expect(root.querySelector('[data-e2e="local-model-awaiting-approval"]')).toBeNull();
      expect(root.textContent).toContain("qwen2.5:7b");
    } finally {
      vi.useRealTimers();
    }
  });

  it("says so when Imbue Studio on the user's computer does not answer", async () => {
    findLocalModelServers.mockResolvedValue(found([], "UNREACHABLE"));

    const { root } = await openForm();

    expect(root.querySelector('[data-e2e="local-model-none"]')?.textContent).toContain(
      "Imbue Studio on your computer did not answer",
    );
  });

  it("says to start a model server when the user's computer runs none", async () => {
    findLocalModelServers.mockResolvedValue(found([], "CONNECTED"));

    const { root } = await openForm();

    const hint = root.querySelector('[data-e2e="local-model-none"]')?.textContent;
    expect(hint).toContain("No model server is running on your computer");
    expect(hint).toContain(
      "You may need to install Ollama: Imbue Studio can do it for you in its Settings, under Local models.",
    );
    expect(hint).toContain("Open Local models settings");
  });

  it("asks Imbue Studio to open its Local models settings from the hint's link", async () => {
    findLocalModelServers.mockResolvedValue(found([], "CONNECTED"));
    const posted: unknown[] = [];
    const post = vi.spyOn(window.parent, "postMessage").mockImplementation((message: unknown) => {
      posted.push(message);
    });

    const { root } = await openForm();
    root.querySelector<HTMLButtonElement>('[data-e2e="open-local-models-settings"]')?.click();
    post.mockRestore();

    expect(posted).toEqual([{ type: "minds:open-local-models-settings" }]);
  });

  it("asks for an address when no server answers, and looks at the one named", async () => {
    findLocalModelServers.mockResolvedValueOnce(found([]));
    findLocalModelServers.mockResolvedValueOnce(found([{ ...OLLAMA, base_url: "http://box.lan:9000/v1" }]));

    const { root, draw } = await openForm();

    expect(root.textContent).toContain("No model server is running on your computer. You may need to install Ollama");
    expect((root.querySelector('[data-e2e="connect-local-model"]') as HTMLButtonElement).disabled).toBe(true);
    type(root, "local-model-address", "box.lan:9000");
    type(root, "local-model-key", "sk-secret");
    draw();
    press(root, "local-model-find");
    await settled();
    draw();
    expect(findLocalModelServers).toHaveBeenLastCalledWith("box.lan:9000", "sk-secret");
    press(root, "connect-local-model");
    expect(submitEndpoint).toHaveBeenCalledExactlyOnceWith({
      base_url: "http://box.lan:9000/v1",
      model_id: "qwen2.5:7b",
      context_window: 32768,
      api_key: "sk-secret",
      studio_port: null,
    });
  });

  it("says why a named server gave no models, and keeps the form up", async () => {
    findLocalModelServers.mockResolvedValueOnce(found([]));
    findLocalModelServers.mockRejectedValueOnce({
      response: { detail: "No server answered at http://box.lan:9000/v1." },
    });

    const { root, draw } = await openForm();
    type(root, "local-model-address", "box.lan:9000");
    draw();
    press(root, "local-model-find");
    await settled();
    draw();

    expect(root.querySelector('[data-e2e="local-model-error"]')?.textContent).toBe(
      "No server answered at http://box.lan:9000/v1.",
    );
    expect(root.querySelector('[data-e2e="local-model-form"]')).not.toBeNull();
  });

  it("looks again on request after a look that could not reach the workspace, saying so plainly", async () => {
    // What a request reports when the proxy in front of the workspace answers with no JSON body.
    findLocalModelServers.mockRejectedValueOnce(Object.assign(new Error("null"), { code: 503, response: null }));
    findLocalModelServers.mockResolvedValueOnce(found([OLLAMA]));

    const { root, draw } = await openForm();

    expect(root.querySelector('[data-e2e="local-model-error"]')?.textContent).toBe(
      "Could not reach the workspace. Try again.",
    );
    press(root, "local-model-look-again");
    await settled();
    draw();
    expect(findLocalModelServers).toHaveBeenLastCalledWith(null, "");
    expect(root.querySelector('[data-e2e="local-model-error"]')).toBeNull();
    expect(root.textContent).toContain("qwen2.5:7b");
    expect((root.querySelector('[data-e2e="connect-local-model"]') as HTMLButtonElement).disabled).toBe(false);
  });

  it("says a server with no models needs one added", async () => {
    findLocalModelServers.mockResolvedValue(found([{ ...OLLAMA, models: [], choices: [] }]));

    const { root } = await openForm();

    expect(root.querySelector('[data-e2e="local-model-error"]')?.textContent).toContain(
      "Ollama answered but lists no models",
    );
  });

  it("will not connect with a context window too small to hold the workspace's instructions", async () => {
    findLocalModelServers.mockResolvedValue(found([OLLAMA]));

    const { root, draw } = await openForm();
    type(root, "local-model-context", "4096");
    draw();

    expect((root.querySelector('[data-e2e="connect-local-model"]') as HTMLButtonElement).disabled).toBe(true);
    press(root, "connect-local-model");
    expect(submitEndpoint).not.toHaveBeenCalled();
  });

  it("offers a model once with its context sizes to choose from, starting on the largest within the default", async () => {
    findLocalModelServers.mockResolvedValue({
      ...(found([DESCRIBED_OLLAMA]) as object),
      default_context_window: 65536,
    });

    const { root, draw } = await openForm();

    // The size is one of the model's own, so there is nothing to type.
    expect(root.querySelector('[data-e2e="local-model-context"]')).toBeNull();
    const sizeTrigger = root.querySelector('[aria-label="Context window"]') as HTMLElement;
    expect(sizeTrigger.textContent).toContain("64K tokens");
    sizeTrigger.click();
    draw();
    expect(
      [...document.querySelectorAll("[data-dropdown-option]")].map((option) =>
        option.getAttribute("data-dropdown-option"),
      ),
    ).toEqual(["40960", "65536", "131072"]);
    (document.querySelector('[data-dropdown-option="131072"]') as HTMLElement).click();
    draw();
    press(root, "connect-local-model");

    expect(submitEndpoint).toHaveBeenCalledExactlyOnceWith({
      base_url: "http://host.docker.internal:11434/v1",
      model_id: "llama3.3:latest",
      context_window: 131072,
      api_key: "",
      studio_port: null,
    });
  });

  it("names the models that cannot run a chat, with why, apart from the ones to pick", async () => {
    findLocalModelServers.mockResolvedValue(found([DESCRIBED_OLLAMA]));

    const { root, draw } = await openForm();

    const unusable = root.querySelector('[data-e2e="local-model-unusable"]') as HTMLElement;
    expect(unusable.querySelector("summary")?.textContent).toBe("1 other model cannot run a chat here");
    expect(unusable.textContent).toContain("qwen2.5:7b: It takes a context of 32,768 tokens at most");
    (root.querySelector('[aria-label="Model"]') as HTMLElement).click();
    draw();
    expect(document.querySelectorAll('[data-dropdown-part="list"] [data-dropdown-option]')).toHaveLength(1);
    (root.querySelector('[aria-label="Model"]') as HTMLElement).click();
    draw();
  });

  it("says so when a server's models are all unable to run a chat", async () => {
    findLocalModelServers.mockResolvedValue(found([{ ...DESCRIBED_OLLAMA, choices: [DESCRIBED_OLLAMA.choices[1]] }]));

    const { root } = await openForm();

    expect(root.querySelector('[data-e2e="local-model-error"]')?.textContent).toContain(
      "Ollama answered, but none of its models can run a chat here",
    );
    expect(root.querySelector('[data-e2e="local-model-unusable"]')).not.toBeNull();
  });

  it("offers a different server while one was found", async () => {
    findLocalModelServers.mockResolvedValue(found([OLLAMA]));

    const { root, draw } = await openForm();
    expect(root.querySelector('[data-e2e="local-model-address"]')).toBeNull();
    press(root, "local-model-name-server");
    draw();

    expect(root.querySelector('[data-e2e="local-model-address"]')).not.toBeNull();
  });
});
