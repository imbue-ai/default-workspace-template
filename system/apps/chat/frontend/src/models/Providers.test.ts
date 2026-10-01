// @vitest-environment jsdom
//
// A terminal sign-in polls on `window.setInterval`, which the sign-in tests below drive.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend without a real network
// call. redraw is a no-op; apiUrl is identity so URLs are predictable.
const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

import { RECONNECT_BASE_MS } from "@imbue/workspace-ui/src/models/backoff";
import {
  closeProviderChooser,
  getAccounts,
  getFlow,
  isProviderChooserOpen,
  loadAccountsWithRetry,
  openProviderChooser,
  pickAccount,
  startFlow,
  submitKey,
} from "./Providers";

const ACCOUNTS_BODY = {
  accounts: [{ id: "acct-1", lane: "claude", harness: "claude", provider: "Anthropic", name: "" }],
  mru: "acct-1",
};

describe("loadAccountsWithRetry", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockRequest.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads immediately when the first fetch succeeds", async () => {
    mockRequest.mockResolvedValueOnce(ACCOUNTS_BODY);

    await loadAccountsWithRetry();

    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(getAccounts().map((account) => account.id)).toEqual(["acct-1"]);
  });

  it("retries a failing fetch until the backend answers", async () => {
    mockRequest
      .mockRejectedValueOnce(new Error("backend still booting"))
      .mockRejectedValueOnce(new Error("backend still booting"))
      .mockResolvedValueOnce(ACCOUNTS_BODY);

    const settled = vi.fn();
    void loadAccountsWithRetry().then(settled);

    // One failed attempt so far; the retry is waiting out its backoff delay.
    await vi.advanceTimersByTimeAsync(0);
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(settled).not.toHaveBeenCalled();

    // Each advance covers exactly one attempt's jittered worst case (base * 2^attempt
    // * 1.2 jitter), so the retries are observed firing one at a time.
    await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS * 1.3);
    expect(mockRequest).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS * 2 * 1.3);
    expect(mockRequest).toHaveBeenCalledTimes(3);
    expect(settled).toHaveBeenCalled();
    expect(getAccounts().map((account) => account.id)).toEqual(["acct-1"]);
  });
});

describe("whenAccountsReadyToChoose", () => {
  // A fresh module per test: the first account read it waits for is module state.
  let providers: typeof import("./Providers");

  beforeEach(async () => {
    mockRequest.mockReset();
    vi.resetModules();
    providers = await import("./Providers");
  });

  it("settles only once the account list has been read, so a new chat never decides on the empty list", async () => {
    let answer: (body: typeof ACCOUNTS_BODY) => void = () => {};
    mockRequest.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    const settled = vi.fn();
    const ready = providers.whenAccountsReadyToChoose().then(settled);

    const loading = providers.loadAccounts();
    await Promise.resolve();
    expect(settled).not.toHaveBeenCalled();
    expect(providers.getSelectedAccount()).toBeNull();

    answer(ACCOUNTS_BODY);
    await loading;
    await ready;
    expect(settled).toHaveBeenCalledOnce();
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(providers.getSelectedAccount()?.id).toBe("acct-1");
  });

  it("reads the list again when the first read named no account, so a sign-in on another page counts", async () => {
    mockRequest.mockResolvedValueOnce({ accounts: [], mru: null }).mockResolvedValueOnce(ACCOUNTS_BODY);
    await providers.loadAccounts();
    expect(providers.getSelectedAccount()).toBeNull();

    await providers.whenAccountsReadyToChoose();

    expect(mockRequest).toHaveBeenCalledTimes(2);
    expect(providers.getSelectedAccount()?.id).toBe("acct-1");
  });

  it("does not read the list again when it already names an account", async () => {
    mockRequest.mockResolvedValueOnce(ACCOUNTS_BODY);
    await providers.loadAccounts();

    await providers.whenAccountsReadyToChoose();

    expect(mockRequest).toHaveBeenCalledTimes(1);
  });

  it("chooses from the list it has when the second read fails", async () => {
    mockRequest.mockResolvedValueOnce({ accounts: [], mru: null }).mockRejectedValueOnce(new Error("offline"));
    await providers.loadAccounts();

    await expect(providers.whenAccountsReadyToChoose()).resolves.toBeUndefined();
    expect(providers.getSelectedAccount()).toBeNull();
  });
});

describe("isAccountSignedOut", () => {
  // A fresh module per test: the list and the accounts held signed out are module state.
  let providers: typeof import("./Providers");
  const EMPTY_ACCOUNTS_BODY = { accounts: [], mru: null };

  beforeEach(async () => {
    mockRequest.mockReset();
    vi.resetModules();
    providers = await import("./Providers");
  });

  it("does not call an account signed in on another page signed out, and reads the list again to find it", async () => {
    // A chat page read its list before the chat list's own chooser signed in to acct-1 and launched this chat on it.
    mockRequest.mockResolvedValueOnce(EMPTY_ACCOUNTS_BODY).mockResolvedValueOnce(ACCOUNTS_BODY);
    await providers.loadAccounts();

    expect(providers.isAccountSignedOut("acct-1")).toBe(false);
    providers.checkChatAccount("acct-1");

    await vi.waitFor(() => expect(providers.accountForAgent("acct-1")?.id).toBe("acct-1"));
    expect(mockRequest).toHaveBeenCalledTimes(2);
    expect(providers.isAccountSignedOut("acct-1")).toBe(false);
  });

  it("calls an account signed out once a fresh read of the list still lacks it", async () => {
    mockRequest.mockResolvedValueOnce(EMPTY_ACCOUNTS_BODY).mockResolvedValueOnce(EMPTY_ACCOUNTS_BODY);
    await providers.loadAccounts();

    providers.checkChatAccount("acct-1");

    await vi.waitFor(() => expect(providers.isAccountSignedOut("acct-1")).toBe(true));
    expect(mockRequest).toHaveBeenCalledTimes(2);
  });

  it("reads the list once for an account however many chat list pushes ask", async () => {
    let answer: (body: typeof EMPTY_ACCOUNTS_BODY) => void = () => {};
    mockRequest
      .mockResolvedValueOnce(EMPTY_ACCOUNTS_BODY)
      .mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    await providers.loadAccounts();

    providers.checkChatAccount("acct-1");
    providers.checkChatAccount("acct-1");
    expect(mockRequest).toHaveBeenCalledTimes(2);

    answer(EMPTY_ACCOUNTS_BODY);
    await vi.waitFor(() => expect(providers.isAccountSignedOut("acct-1")).toBe(true));
    providers.checkChatAccount("acct-1");
    expect(mockRequest).toHaveBeenCalledTimes(2);
  });

  it("reads nothing for an account the list names, or before the list's first read", async () => {
    providers.checkChatAccount("acct-1");
    expect(mockRequest).not.toHaveBeenCalled();

    mockRequest.mockResolvedValueOnce(ACCOUNTS_BODY);
    await providers.loadAccounts();
    providers.checkChatAccount("acct-1");
    providers.checkChatAccount(null);

    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(providers.isAccountSignedOut("acct-1")).toBe(false);
  });

  it("leaves the account signed in when the fresh read fails, and reads again on the next check", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    mockRequest
      .mockResolvedValueOnce(EMPTY_ACCOUNTS_BODY)
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(ACCOUNTS_BODY);
    await providers.loadAccounts();

    providers.checkChatAccount("acct-1");
    await vi.waitFor(() => expect(consoleError).toHaveBeenCalledOnce());
    expect(providers.isAccountSignedOut("acct-1")).toBe(false);

    providers.checkChatAccount("acct-1");
    await vi.waitFor(() => expect(providers.accountForAgent("acct-1")?.id).toBe("acct-1"));
    expect(providers.isAccountSignedOut("acct-1")).toBe(false);
    consoleError.mockRestore();
  });

  it("calls an account this page removed signed out at once, and signed in again once a read names it", async () => {
    mockRequest
      .mockResolvedValueOnce(ACCOUNTS_BODY)
      .mockResolvedValueOnce(undefined)
      .mockResolvedValueOnce(EMPTY_ACCOUNTS_BODY)
      .mockResolvedValueOnce(ACCOUNTS_BODY);
    await providers.loadAccounts();

    await providers.deleteAccount("acct-1");
    expect(providers.isAccountSignedOut("acct-1")).toBe(true);

    await providers.loadAccounts();
    expect(providers.isAccountSignedOut("acct-1")).toBe(false);
  });

  it("calls the account signed out when a re-read after a refused send lacks it", async () => {
    // The send was refused because another window removed acct-1, which this page's list still names.
    mockRequest.mockResolvedValueOnce(ACCOUNTS_BODY).mockResolvedValueOnce(EMPTY_ACCOUNTS_BODY);
    await providers.loadAccounts();
    expect(providers.isAccountSignedOut("acct-1")).toBe(false);

    await providers.rereadAccountsFor("acct-1");

    expect(providers.isAccountSignedOut("acct-1")).toBe(true);
  });
});

describe("startFlow", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockRequest.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("holds no flow while the next sign-in is being started", async () => {
    // Picking a method under "Other ways to sign in" starts a second flow, and the paste
    // screen renders before that flow exists. A flow held across the window belongs to the
    // method just left -- the server has already dropped it, and it took a code, not a key --
    // so anything submitted there is refused.
    mockRequest.mockResolvedValueOnce({
      flow_id: "flow-subscription",
      shape: "code_then_wait",
      url: "https://example.invalid/login",
      code: "ABCD-1234",
    });
    await startFlow("anthropic", "subscription");
    expect(getFlow()?.flow_id).toBe("flow-subscription");

    let arrive: (value: unknown) => void = () => {};
    mockRequest.mockReturnValueOnce(
      new Promise((resolve) => {
        arrive = resolve;
      }),
    );
    const started = startFlow("anthropic", "api_key");

    expect(getFlow()).toBeNull();

    arrive({ flow_id: "flow-api-key", shape: "paste", url: null, code: null });
    await started;
    expect(getFlow()?.flow_id).toBe("flow-api-key");
  });
});

describe("the provider chooser's hooks", () => {
  // A seeded chat's first send waits on the chooser: the sign-in hook launches it, the
  // dismissal hook puts the message back. Which one runs decides whether the message is sent
  // once, twice, or lost.
  beforeEach(() => {
    mockRequest.mockReset();
  });

  afterEach(() => {
    closeProviderChooser();
  });

  it("runs the dismissal hook once when it closes with the sign-in hook still armed", () => {
    const onSignedIn = vi.fn();
    const onDismissed = vi.fn();
    openProviderChooser({ onSignedIn, onDismissed });
    expect(isProviderChooserOpen()).toBe(true);

    closeProviderChooser();

    expect(onDismissed).toHaveBeenCalledTimes(1);
    expect(onSignedIn).not.toHaveBeenCalled();
    // A second close of a closed chooser, and a chooser opened without hooks, run nothing.
    closeProviderChooser();
    openProviderChooser();
    closeProviderChooser();
    expect(onDismissed).toHaveBeenCalledTimes(1);
  });

  it("runs the sign-in hook when a sign-in lands, and the close that follows runs no dismissal", async () => {
    const onSignedIn = vi.fn();
    const onDismissed = vi.fn();
    openProviderChooser({ onSignedIn, onDismissed });
    // A paste flow: nothing polls, the submitted key settles it.
    mockRequest.mockResolvedValueOnce({ flow_id: "flow-api-key", shape: "paste", url: null, code: null });
    await startFlow("anthropic", "api_key");
    mockRequest
      .mockResolvedValueOnce({ state: "ok", detail: null, account_id: "acct-1" })
      .mockResolvedValueOnce(ACCOUNTS_BODY);

    await submitKey("sk-test", null);

    expect(onSignedIn).toHaveBeenCalledWith("acct-1");
    expect(onDismissed).not.toHaveBeenCalled();
    closeProviderChooser();
    expect(onDismissed).not.toHaveBeenCalled();
    expect(onSignedIn).toHaveBeenCalledTimes(1);
  });

  it("runs the sign-in hook when a signed-in account is picked, and the close that follows runs no dismissal", () => {
    const onSignedIn = vi.fn();
    const onDismissed = vi.fn();
    openProviderChooser({ onSignedIn, onDismissed });

    pickAccount("acct-1");

    expect(isProviderChooserOpen()).toBe(false);
    expect(onSignedIn).toHaveBeenCalledWith("acct-1");
    expect(onDismissed).not.toHaveBeenCalled();
    closeProviderChooser();
    expect(onDismissed).not.toHaveBeenCalled();
    expect(onSignedIn).toHaveBeenCalledTimes(1);
  });
});
