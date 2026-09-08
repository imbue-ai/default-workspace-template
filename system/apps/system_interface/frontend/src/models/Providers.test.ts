// @vitest-environment jsdom
//
// A terminal sign-in polls on `window.setInterval`, which the sign-in tests below drive.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend without a real network
// call. redraw is a no-op; apiUrl is identity so URLs are predictable.
const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import { RECONNECT_BASE_MS } from "./backoff";
import { areAccountsLoaded, getAccounts, getFlow, loadAccountsWithRetry, startFlow } from "./Providers";

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
    expect(areAccountsLoaded()).toBe(true);
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
