// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";

import { fileEntry } from "../testing/records";
import { FilesController } from "./FilesController";

const HOME = "/home/";

function framedConnection(): ShellConnection {
  const connection: ShellConnection = {
    isFramed: true,
    hasShell: () => true,
    focused: vi.fn(),
    location: vi.fn(),
    openPath: vi.fn(),
    startWithText: vi.fn(),
    canReportBug: () => false,
    reportBug: vi.fn(),
    draftText: vi.fn(),
    sendMessage: vi.fn(),
    openLink: vi.fn(),
    disconnect: vi.fn(),
  };
  return connection;
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ shares: [], syncs: [] }), { status: 200 })),
  );
  window.matchMedia = vi.fn(
    (query: string) =>
      ({
        matches: false,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }) as unknown as MediaQueryList,
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("opening a file in a window of its own", () => {
  it("asks the shell for the file's window", () => {
    const connection = framedConnection();
    const app = new FilesController(HOME, { exists: true, entries: [] }, connection);

    app.openPage("/home/notes.md", "view");

    expect(connection.openPath).toHaveBeenCalledWith("/home/notes.md?view", "focus");
    expect(app.model.toasts.current()).toEqual([]);
  });

  it("says a file whose location is too long for a window is to be downloaded, and asks the shell for nothing", () => {
    const connection = framedConnection();
    const deep = `${HOME}${"a".repeat(2100)}/`;
    const app = new FilesController(deep, { exists: true, entries: [fileEntry(deep, "notes.md")] }, connection);

    app.openPage(`${deep}notes.md`, "view");

    expect(connection.openPath).not.toHaveBeenCalled();
    expect(app.model.toasts.current().map((toast) => toast.message)).toEqual([
      "This file's location is too long to open in a window of its own. Download it from the folder instead.",
    ]);
  });
});
