// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

import type { ShellConnection, StartOutcome, StartOptions } from "@imbue/workspace-ui/src/app_contract";
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
    askAgent: vi.fn(),
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
  // The view a test chose is kept with the other preferences.
  window.localStorage.clear();
  Reflect.deleteProperty(Element.prototype, "scrollTo");
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
    const lateAnswers: ((outcome: StartOutcome) => void)[] = [];
    const connection: ShellConnection = {
      ...framedConnection(),
      startWithText: vi.fn(async (_text: string, options?: StartOptions) => {
        if (options?.onLateAnswer !== undefined) lateAnswers.push(options.onLateAnswer);
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

  const LISTING_REASON = "GET /home/: [Errno 5] Input/output error";

  /** A Files window on a folder the server answers `listing` for, shown as `viewMode` lays it out. */
  async function openedOn(
    listing: { status: number; body: Record<string, unknown> },
    connection: ShellConnection,
    viewMode: "icons" | "columns" = "icons",
  ): Promise<HTMLElement> {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.endsWith("?json") ? new Response(JSON.stringify(listing.body), { status: listing.status }) : answered(url),
      ),
    );
    const app = new FilesController(HOME, undefined, connection);
    app.model.setViewMode(viewMode);
    app.model.start();
    return mountView(() => m(FilesApp, { app }));
  }

  async function failedListing(canReportBug: boolean): Promise<{ connection: ShellConnection; root: HTMLElement }> {
    const connection = { ...framedConnection(), canReportBug: () => canReportBug };
    const root = await openedOn({ status: 500, body: { detail: LISTING_REASON } }, connection);
    await vi.waitFor(() => expect(root.querySelector(".files-empty")?.textContent).toContain(LISTING_FAILED));
    return { connection, root };
  }

  function buttonsIn(root: HTMLElement, selector: string): (string | null)[] {
    return Array.from(root.querySelectorAll(`${selector} button`), (button) => button.textContent);
  }

  it("offers Report a bug carrying the sentence and the server's reason where Imbue Studio can open one", async () => {
    const { connection, root } = await failedListing(true);

    root.querySelector<HTMLButtonElement>(".files-empty .report-bug")?.click();

    // The report names the folder, which the window shows rather than says in its sentence.
    expect(connection.reportBug).toHaveBeenCalledWith({
      message: LISTING_FAILED,
      details: "/home/\nHTTP 500\nGET /home/: [Errno 5] Input/output error",
    });
  });

  it("offers Try again and an agent, but no Report a bug, where Imbue Studio cannot open a bug report", async () => {
    const { root } = await failedListing(false);

    expect(buttonsIn(root, ".files-empty")).toEqual(["Try again", "Ask an agent to fix this"]);
  });

  it("asks an agent to fix it through the shell, and one Report a bug files a refused ask with it", async () => {
    let answer: (outcome: StartOutcome) => void = () => undefined;
    const askAgent = vi.fn(
      (_text: string, _options?: StartOptions) => new Promise<StartOutcome>((resolve) => (answer = resolve)),
    );
    const connection = { ...framedConnection(), canReportBug: () => true, askAgent };
    const root = await openedOn({ status: 500, body: { detail: LISTING_REASON } }, connection);
    await vi.waitFor(() =>
      expect(buttonsIn(root, ".files-empty")).toEqual(["Try again", "Ask an agent to fix this", "Report a bug"]),
    );

    const ask = root.querySelector(".files-empty .ask-agent-to-fix") as HTMLButtonElement;
    ask.click();
    m.redraw.sync();

    expect([ask.textContent, ask.disabled]).toEqual(["Asking an agent…", true]);
    expect(connection.startWithText).not.toHaveBeenCalled();
    const [task, options] = askAgent.mock.calls[0];
    expect(task).toMatch(/^Imbue Studio showed the user this failure: Couldn't open this folder\./);
    expect(task).toContain(`/home/\nHTTP 500\n${LISTING_REASON}`);
    expect(options?.showsOutcome).toBe(true);

    answer({
      isOpened: false,
      failure: { message: "Chat couldn't open that.", details: null, isReportable: false },
    });
    await vi.waitFor(() => expect(ask.disabled).toBe(false));
    m.redraw.sync();
    // FailureActions puts the refused ask under the buttons by this class, as a child of its row; jsdom lays nothing
    // out, so only this lookup says when the shared button stops drawing it that way.
    expect(
      root.querySelector(".files-failure-actions > .ask-agent-failure .ask-agent-failure-message")?.textContent,
    ).toBe("Chat couldn't open that.");
    root.querySelector<HTMLButtonElement>(".files-empty .report-bug")?.click();
    expect(connection.reportBug).toHaveBeenCalledWith({
      message: LISTING_FAILED,
      details: `/home/\nHTTP 500\n${LISTING_REASON}\n\nAsking an agent to fix this failed too:\nChat couldn't open that.`,
    });
  });

  it("keeps a pending ask while the folder is listed again and fails the same way", async () => {
    const askAgent = vi.fn((_text: string, _options?: StartOptions) => new Promise<StartOutcome>(() => undefined));
    const connection = { ...framedConnection(), askAgent };
    const root = await openedOn({ status: 500, body: { detail: LISTING_REASON } }, connection);
    await vi.waitFor(() => expect(root.querySelector(".files-empty .ask-agent-to-fix")).not.toBeNull());
    root.querySelector<HTMLButtonElement>(".files-empty .ask-agent-to-fix")?.click();
    m.redraw.sync();
    const listings = (): number => vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("?json")).length;
    const listedBefore = listings();

    // A window coming back to the front lists its folder again.
    window.dispatchEvent(new Event("focus"));
    await vi.waitFor(() => expect(listings()).toBeGreaterThan(listedBefore));
    await vi.waitFor(() => expect(root.querySelector(".files-empty")?.textContent).toContain(LISTING_FAILED));
    m.redraw.sync();

    const ask = root.querySelector(".files-empty .ask-agent-to-fix") as HTMLButtonElement;
    expect([ask.textContent, ask.disabled]).toEqual(["Asking an agent…", true]);
    expect(askAgent).toHaveBeenCalledTimes(1);
  });

  it("drops a refused ask once the folder fails another way, and files only the failure on show", async () => {
    const askAgent = vi.fn(async (_text: string, _options?: StartOptions): Promise<StartOutcome> => ({
      isOpened: false,
      failure: { message: "Chat couldn't open that.", details: null, isReportable: false },
    }));
    const connection = { ...framedConnection(), canReportBug: () => true, askAgent };
    const listing = { status: 500, body: { detail: LISTING_REASON } };
    const root = await openedOn(listing, connection);
    await vi.waitFor(() => expect(root.querySelector(".files-empty .ask-agent-to-fix")).not.toBeNull());
    root.querySelector<HTMLButtonElement>(".files-empty .ask-agent-to-fix")?.click();
    await vi.waitFor(() => {
      m.redraw.sync();
      expect(root.querySelector(".files-empty .ask-agent-failure")).not.toBeNull();
    });

    const otherReason = "GET /home/: [Errno 116] Stale file handle";
    listing.body = { detail: otherReason };
    window.dispatchEvent(new Event("focus"));
    await vi.waitFor(() => {
      m.redraw.sync();
      expect(root.querySelector(".files-empty .see-details pre")?.textContent).toBe(`HTTP 500\n${otherReason}`);
    });

    expect(root.querySelector(".files-empty .ask-agent-failure")).toBeNull();
    root.querySelector<HTMLButtonElement>(".files-empty .report-bug")?.click();
    expect(connection.reportBug).toHaveBeenCalledWith({
      message: LISTING_FAILED,
      details: `/home/\nHTTP 500\n${otherReason}`,
    });
  });

  it("offers an agent for a column whose folder cannot be listed", async () => {
    // jsdom lays nothing out, so it scrolls nothing; the column view scrolls its last column into view.
    Element.prototype.scrollTo = vi.fn();
    const connection = { ...framedConnection(), canReportBug: () => true, askAgent: vi.fn() };
    const root = await openedOn({ status: 500, body: { detail: LISTING_REASON } }, connection, "columns");

    // Every column's folder fails to list here; each says so with its own buttons.
    await vi.waitFor(() =>
      expect(buttonsIn(root, ".files-column:last-child .files-column-failure")).toEqual([
        "Ask an agent to fix this",
        "Report a bug",
      ]),
    );
  });

  it("offers no agent for a folder it has no permission to open", async () => {
    const connection = { ...framedConnection(), canReportBug: () => true, askAgent: vi.fn() };
    const root = await openedOn(
      {
        status: 403,
        body: {
          detail: "GET /home/: [Errno 13]",
          user_facing_message: "You don't have permission to change or read this.",
          kind: "permission_denied",
        },
      },
      connection,
    );

    await vi.waitFor(() =>
      expect(root.querySelector(".files-empty")?.textContent).toContain("You don't have permission to open it."),
    );
    expect(buttonsIn(root, ".files-empty")).toEqual(["Try again"]);
  });
});

describe("what your computers share, when it cannot be read", () => {
  it("offers an agent, then Report a bug, under a failure that asks for one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.includes("/__files/api/connections")
          ? new Response(JSON.stringify({ detail: "gateway: [Errno 5] Input/output error" }), { status: 500 })
          : answered(url),
      ),
    );
    const askAgent = vi.fn(async (_text: string, _options?: StartOptions): Promise<StartOutcome> => ({
      isOpened: true,
    }));
    const connection = { ...framedConnection(), canReportBug: () => true, askAgent };
    const app = new FilesController(HOME, undefined, connection);
    app.model.start();
    const root = mountView(() => m(FilesApp, { app }));
    const said = "Couldn't read what your computers share. Try again. If this persists, report a bug.";

    await vi.waitFor(() => {
      m.redraw.sync();
      expect(root.querySelector(".files-connected-empty")?.textContent).toContain(said);
    });
    const buttons = Array.from(root.querySelectorAll(".files-connected-empty button"), (button) => button.textContent);
    expect(buttons).toEqual(["Ask an agent to fix this", "Report a bug"]);

    root.querySelector<HTMLButtonElement>(".files-connected-empty .ask-agent-to-fix")?.click();
    const [task] = askAgent.mock.calls[0];
    expect(task).toMatch(/^Imbue Studio showed the user this failure: Couldn't read what your computers share\./);
    expect(task).toContain("HTTP 500\ngateway: [Errno 5] Input/output error");
    root.querySelector<HTMLButtonElement>(".files-connected-empty .report-bug")?.click();
    expect(connection.reportBug).toHaveBeenCalledWith({
      message: said,
      details: "HTTP 500\ngateway: [Errno 5] Input/output error",
    });
  });
});
