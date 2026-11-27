// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";

const CAPABILITIES = { type: "minds:embedder-capabilities", canPopOut: false };

let connection: ShellConnection | null = null;

/** Frame this window under a spy parent for the duration of the test. */
function framed(): { postMessage: ReturnType<typeof vi.fn> } {
  const parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  return parent;
}

function deliver(data: unknown, source: unknown): void {
  window.dispatchEvent(new MessageEvent("message", { data, source: source as Window }));
}

afterEach(() => {
  connection?.disconnect();
  connection = null;
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

describe("the first run's cover", () => {
  it("keeps whether the chrome relays no provider sign-in, as the chrome's own message says it", async () => {
    const parent = framed();
    vi.resetModules();
    const cover = await import("./firstRunCover");
    const relay = await import("../models/providerRelay");
    connection = cover.connectFirstRunCoverToShell();
    expect(relay.isProviderRelayDeclined()).toBe(false);

    deliver({ ...CAPABILITIES, declinesProviderRelay: true }, parent);
    expect(relay.isProviderRelayDeclined()).toBe(true);

    deliver(CAPABILITIES, parent);
    expect(relay.isProviderRelayDeclined()).toBe(false);
  });
});
