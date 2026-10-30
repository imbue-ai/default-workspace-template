/**
 * A chat page framed by a shell, for view tests: a real ``connectToShell`` under a spy parent window, which has sent
 * its handshake, made the page's connection (``setShellConnection``), and records what the page posts to it.
 */

import { vi } from "vitest";
import { SHELL_HANDSHAKE, connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { setShellConnection } from "./shellConnection";

export interface FramedPage {
  readonly connection: ShellConnection;
  /** The message types the page posted to the shell, in order. */
  postedTypes(): string[];
  /** The posted messages of ``type``. */
  posted(type: string): Record<string, unknown>[];
  /** Unframe the page again. */
  release(): void;
}

export function framePageInShell(canReportBug: boolean): FramedPage {
  const parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  const connection = connectToShell({});
  window.dispatchEvent(
    new MessageEvent("message", {
      data: {
        type: SHELL_HANDSHAKE,
        clientId: "client-1",
        windowId: "win-1",
        desktopId: "home",
        app: "chat",
        canReportBug,
      },
      source: parent as unknown as Window,
    }),
  );
  setShellConnection(connection);
  const messages = (): Record<string, unknown>[] =>
    parent.postMessage.mock.calls.map(([message]) => message as Record<string, unknown>);
  return {
    connection,
    postedTypes: () => messages().map((message) => String(message.type)),
    posted: (type) => messages().filter((message) => message.type === type),
    release: () => {
      connection.disconnect();
      Object.defineProperty(window, "parent", { value: window, configurable: true });
    },
  };
}

/** A connection with every call a spy, for a test without a DOM to frame a page in: made the page's connection. */
export function stubShellConnection(canReportBug: boolean): ShellConnection {
  const connection: ShellConnection = {
    isFramed: true,
    hasShell: () => true,
    focused: vi.fn(),
    location: vi.fn(),
    openPath: vi.fn(),
    startWithText: vi.fn(),
    canReportBug: () => canReportBug,
    reportBug: vi.fn(),
    draftText: vi.fn(),
    sendMessage: vi.fn(),
    openLink: vi.fn(),
    disconnect: vi.fn(),
  };
  setShellConnection(connection);
  return connection;
}
