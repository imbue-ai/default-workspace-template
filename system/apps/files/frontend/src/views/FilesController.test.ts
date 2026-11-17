// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

import type { ShellConnection, StartNewOutcome } from "@imbue/workspace-ui/src/app_contract";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import { fileEntry } from "../testing/records";
import { FilesApp } from "./FilesApp";
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
    startNewWithText: vi.fn(),
    canReportBug: () => false,
    reportBug: vi.fn(),
    draftText: vi.fn(),
    sendMessage: vi.fn(),
    openLink: vi.fn(),
    disconnect: vi.fn(),
  };
  return connection;
}

/** What the server answers a request that succeeds: the folders' stamps, else the shares (an empty set of each). */
function answered(url: string): Response {
  const body = url.includes("/__files/api/stamps") ? { stamps: {} } : { shares: [], syncs: [] };
  return new Response(JSON.stringify(body), { status: 200 });
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => answered(url)),
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
  unmountViews();
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

describe("asking an agent to free up space", () => {
  it("replaces a not-answered note with the shell's late answer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) =>
        init?.method === "MKCOL"
          ? new Response(
              JSON.stringify({
                detail: "MKCOL: [Errno 28]",
                user_facing_message: "This workspace is out of disk space.",
                kind: "out_of_space",
              }),
              { status: 507 },
            )
          : answered(url),
      ),
    );
    const lateAnswers: ((outcome: StartNewOutcome) => void)[] = [];
    const connection: ShellConnection = {
      ...framedConnection(),
      startWithText: vi.fn(async (_text: string, onLateAnswer?: (outcome: StartNewOutcome) => void) => {
        if (onLateAnswer !== undefined) lateAnswers.push(onLateAnswer);
        return { isOpened: false, failure: { message: "No answer came.", details: null, isReportable: false } };
      }),
    };
    const app = new FilesController(HOME, { exists: true, entries: [] }, connection);
    await app.model.createFolder();
    const messages = (): string[] => app.model.toasts.current().map((toast) => toast.message);

    app.model.toasts.current()[0].actions[0].run();
    await vi.waitFor(() => expect(messages()).toContain("No answer came."));

    expect(lateAnswers).toHaveLength(1);
    lateAnswers[0]({ isOpened: true });
    expect(messages()).not.toContain("No answer came.");
  });
});

describe("a folder that cannot be listed", () => {
  const LISTING_FAILED = "Couldn't open this folder. Try again. If this persists, report a bug.";

  async function failedListing(canReportBug: boolean): Promise<{ connection: ShellConnection; root: HTMLElement }> {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.endsWith("?json")
          ? new Response(JSON.stringify({ detail: "GET /home/: [Errno 5] Input/output error" }), { status: 500 })
          : answered(url),
      ),
    );
    const connection = { ...framedConnection(), canReportBug: () => canReportBug };
    const app = new FilesController(HOME, undefined, connection);
    app.model.start();
    const root = mountView(() => m(FilesApp, { app }));
    await vi.waitFor(() => expect(root.querySelector(".files-empty")?.textContent).toContain(LISTING_FAILED));
    return { connection, root };
  }

  it("offers Report a bug carrying the sentence and the server's reason where Imbue Studio can open one", async () => {
    const { connection, root } = await failedListing(true);

    root.querySelector<HTMLButtonElement>(".files-empty .report-bug")?.click();

    expect(connection.reportBug).toHaveBeenCalledWith({
      message: LISTING_FAILED,
      details: "HTTP 500\nGET /home/: [Errno 5] Input/output error",
    });
  });

  it("offers only Try again where Imbue Studio cannot open a bug report", async () => {
    const { root } = await failedListing(false);

    expect(root.querySelector(".files-empty .report-bug")).toBeNull();
    expect(root.querySelector(".files-empty button")?.textContent).toBe("Try again");
  });
});
