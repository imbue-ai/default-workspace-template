import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const embed = vi.hoisted(() => ({
  sent: [] as { type: string; payload?: Record<string, unknown> }[],
  handlers: new Map<string, (message: Record<string, unknown>) => void>(),
}));

vi.mock("@imbue/workspace-ui/src/embed", () => ({
  PROVIDER_SIGN_IN: "minds:provider-sign-in",
  PROVIDER_SIGN_IN_ACK: "minds:provider-sign-in-ack",
  sendToEmbedder: (type: string, payload?: Record<string, unknown>) => embed.sent.push({ type, payload }),
  setEmbedderMessageHandler: (type: string, handler: (message: Record<string, unknown>) => void) =>
    embed.handlers.set(type, handler),
  clearEmbedderMessageHandler: (type: string) => embed.handlers.delete(type),
}));

import { RELAY_ACK_TIMEOUT_MS, requestProviderRelay } from "./providerRelay";

function ack(relay: unknown): void {
  embed.handlers.get("minds:provider-sign-in-ack")?.({ type: "minds:provider-sign-in-ack", relay });
}

beforeEach(() => {
  vi.useFakeTimers();
  embed.sent.length = 0;
  embed.handlers.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("asking the desktop app to relay a sign-in", () => {
  it("hands it the page and the flow", () => {
    void requestProviderRelay("https://claude.ai/oauth/authorize?state=s", "flow-1");

    expect(embed.sent).toEqual([
      {
        type: "minds:provider-sign-in",
        payload: { url: "https://claude.ai/oauth/authorize?state=s", flowId: "flow-1" },
      },
    ]);
  });

  it("takes the desktop app at its word", async () => {
    const relaying = requestProviderRelay("https://claude.ai/x", "flow-1");
    ack(true);
    expect(await relaying).toBe(true);

    const declined = requestProviderRelay("https://claude.ai/x", "flow-2");
    ack(false);
    expect(await declined).toBe(false);
  });

  it("signs in without it when nothing answers in time", async () => {
    const relaying = requestProviderRelay("https://claude.ai/x", "flow-1");

    vi.advanceTimersByTime(RELAY_ACK_TIMEOUT_MS);

    expect(await relaying).toBe(false);
    expect(embed.handlers.has("minds:provider-sign-in-ack")).toBe(false);
  });

  it("reads anything but a true relay as no", async () => {
    const relaying = requestProviderRelay("https://claude.ai/x", "flow-1");
    ack("true");
    expect(await relaying).toBe(false);
  });
});
