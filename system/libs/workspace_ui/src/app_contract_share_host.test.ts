// @vitest-environment jsdom
// @vitest-environment-options {"url": "https://chat-ab12cd34.0123456789abcdef0123456789abcdef.fedcba9876543210fedcba9876543210.us1.example.com/"}
//
// The app contract on a page served from a share address (a direct share visit's frame), whose host is not local:
// another app of the same share is still this workspace's, while any other site is the browser's.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SHELL_HANDSHAKE, SHELL_OPEN_LINK, connectToShell } from "./app_contract";
import type { ShellConnection } from "./app_contract";

const SHARE_DOMAIN = "0123456789abcdef0123456789abcdef.fedcba9876543210fedcba9876543210.us1.example.com";

describe("a framed page on a share address", () => {
  let connection: ShellConnection | null = null;
  let parent: { postMessage: ReturnType<typeof vi.fn> };
  let nativeOpen: ReturnType<typeof vi.fn>;
  let savedOpen: typeof window.open;

  beforeEach(() => {
    savedOpen = window.open;
    nativeOpen = vi.fn(() => null);
    window.open = nativeOpen as unknown as typeof window.open;
    parent = { postMessage: vi.fn() };
    Object.defineProperty(window, "parent", { value: parent, configurable: true });
    connection = connectToShell({});
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: SHELL_HANDSHAKE, clientId: "client-1" },
        source: parent as unknown as Window,
      }),
    );
  });

  afterEach(() => {
    connection?.disconnect();
    connection = null;
    window.open = savedOpen;
    Object.defineProperty(window, "parent", { value: window, configurable: true });
  });

  it("hands a popup to another app of its own share to the shell, and leaves one to another share to the browser", () => {
    const ownShareApp = `https://files-ab12cd34.${SHARE_DOMAIN}/home/user/?view`;
    const otherShareApp = "https://files-ab12cd34.11111111111111111111111111111111.us1.example.com/";

    expect(window.open(ownShareApp, "_blank")).toBeNull();
    window.open(otherShareApp, "_blank");

    expect(parent.postMessage.mock.calls.slice(1)).toEqual([[{ type: SHELL_OPEN_LINK, url: ownShareApp }, "*"]]);
    expect(nativeOpen.mock.calls).toEqual([[otherShareApp, "_blank"]]);
  });
});
