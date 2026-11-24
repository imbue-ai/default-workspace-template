// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import m from "mithril";

import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import type { ReportedFailure, StartOptions, StartOutcome } from "@imbue/workspace-ui/src/app_contract";

import type { FailureHost } from "../models/failures";
import * as server from "../models/server";
import { httpFailure } from "../testing/records";
import { FilePage, FilePageModel } from "./FilePage";

vi.mock("../models/server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/server")>()),
  readText: vi.fn(),
  writeFile: vi.fn(),
  deletePath: vi.fn(),
}));

const PATH = "/home/user/workspace/notes.md";

type Reporter = FailureHost & {
  askAgent: Mock<(text: string, options?: StartOptions) => Promise<StartOutcome>>;
  reportBug: Mock<(failure: ReportedFailure) => void>;
};

function reporter(canReport: boolean, hasShell: boolean): Reporter {
  return {
    hasShell: () => hasShell,
    askAgent: vi.fn(async (): Promise<StartOutcome> => ({ isOpened: true })),
    canReportBug: () => canReport,
    reportBug: vi.fn<(failure: ReportedFailure) => void>(),
  };
}

async function openedPage(
  canReport: boolean,
  hasShell = true,
): Promise<{ page: FilePageModel; root: HTMLElement; host: Reporter }> {
  const host = reporter(canReport, hasShell);
  const page = new FilePageModel({ path: PATH, page: "edit", isText: true }, host);
  await page.loadText();
  const root = mountView(() => m(FilePage, { page }));
  m.redraw.sync();
  return { page, root, host };
}

function failureText(root: HTMLElement): string | null {
  return root.querySelector(".files-load-failure-message")?.textContent ?? null;
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(null, { status: 200 })),
  );
});

afterEach(() => {
  unmountViews();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

const GONE = { detail: "It's no longer there.", user_facing_message: "It's no longer there.", kind: "not_found" };

describe("a file whose text could not be loaded", () => {
  it("says a file the server calls gone was moved or deleted, and offers no agent or bug report", async () => {
    vi.mocked(server.readText).mockRejectedValueOnce(httpFailure(404, GONE, "read"));
    const { root } = await openedPage(true);

    expect(failureText(root)).toBe("This file is no longer there. It may have been moved or deleted.");
    expect(root.querySelector("textarea")).toBeNull();
    expect(root.querySelector(".report-bug")).toBeNull();
    expect(root.querySelector(".ask-agent-to-fix")).toBeNull();
  });

  it("says it couldn't be opened, with the file and the server's text behind See details, and opens it again", async () => {
    vi.mocked(server.readText).mockRejectedValueOnce(httpFailure(500, { detail: "Input/output error" }, "read"));
    const { page, root } = await openedPage(true);

    expect(failureText(root)).toBe("This file couldn't be opened. Try again.");
    // The page shows the file in its title rather than says it in its sentence.
    expect(root.querySelector(".see-details pre")?.textContent).toBe(
      `Tried to open the file ${PATH}\nHTTP 500\nInput/output error`,
    );

    vi.mocked(server.readText).mockResolvedValueOnce("# Notes");
    (root.querySelector('[data-action="retry-load"]') as HTMLButtonElement).click();
    await vi.waitFor(() => expect(page.text.status).toBe("ready"));
    m.redraw.sync();
    expect(failureText(root)).toBeNull();
    expect((root.querySelector("textarea") as HTMLTextAreaElement).value).toBe("# Notes");
  });

  it("offers no Report a bug where Imbue Studio cannot open one", async () => {
    vi.mocked(server.readText).mockRejectedValueOnce(httpFailure(500, null, "read"));
    const { root } = await openedPage(false);

    expect(failureText(root)).toBe("This file couldn't be opened. Try again.");
    expect(root.querySelector(".report-bug")).toBeNull();
    expect(root.querySelector('[data-action="retry-load"]')).not.toBeNull();
  });

  it("asks an agent to fix a file nothing explained, and one Report a bug files a refused ask with it", async () => {
    vi.mocked(server.readText).mockRejectedValueOnce(httpFailure(500, { detail: "Input/output error" }, "read"));
    const { root, host } = await openedPage(true);
    let answer: (outcome: StartOutcome) => void = () => undefined;
    host.askAgent.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
    const buttons = (): (string | null)[] =>
      Array.from(root.querySelectorAll(".files-load-failure button"), (button) => button.textContent);
    expect(buttons()).toEqual(["Try again", "Ask an agent to fix this"]);

    const ask = root.querySelector(".ask-agent-to-fix") as HTMLButtonElement;
    ask.click();
    m.redraw.sync();

    expect([ask.textContent, ask.disabled]).toEqual(["Asking an agent…", true]);
    const [task, options] = host.askAgent.mock.calls[0];
    expect(task).toMatch(/^Imbue Studio showed the user this failure: This file couldn't be opened\./);
    expect(task).toContain(`Tried to open the file ${PATH}\nHTTP 500\nInput/output error`);
    expect(options?.showsOutcome).toBe(true);

    answer({
      isOpened: false,
      failure: { message: "Chat couldn't open that.", details: "HTTP 400\nbad launch", isHelpOwed: true },
    });
    await vi.waitFor(() => expect(ask.disabled).toBe(false));
    m.redraw.sync();
    expect(ask.textContent).toBe("Ask an agent to fix this");
    expect(root.querySelector(".ask-agent-failure-message")?.textContent).toBe("Chat couldn't open that.");
    expect(buttons()).toEqual(["Try again", "Ask an agent to fix this", "Draft it in a chat", "Report a bug"]);
    (root.querySelector(".report-bug") as HTMLButtonElement).click();
    expect(host.reportBug).toHaveBeenCalledWith({
      message: "This file couldn't be opened. Try again.",
      details: `Tried to open the file ${PATH}\nHTTP 500\nInput/output error\n\nAsking an agent to fix this failed too:\nChat couldn't open that.\nHTTP 400\nbad launch`,
    });
  });

  it("offers no agent on a page no shell frames", async () => {
    vi.mocked(server.readText).mockRejectedValueOnce(httpFailure(500, null, "read"));
    const { root } = await openedPage(false, false);

    expect(failureText(root)).toBe("This file couldn't be opened. Try again.");
    expect(root.querySelector(".ask-agent-to-fix")).toBeNull();
  });
});

describe("a file that could not be saved", () => {
  it("shows a refusal that passes on its own as a note with See details, which holds the note up once opened", async () => {
    vi.mocked(server.readText).mockResolvedValueOnce("draft");
    const { page, root } = await openedPage(true);
    page.draft = "draft, changed";
    const said = "It's in use right now. Try again in a moment.";
    vi.mocked(server.writeFile).mockRejectedValueOnce(
      httpFailure(409, {
        detail: "PUT /home/user/workspace/notes.md",
        user_facing_message: said,
        kind: "in_use",
        expected: true,
      }),
    );
    const hold = vi.spyOn(page.toasts, "hold");

    await page.save();
    m.redraw.sync();

    const [toast] = page.toasts.current();
    expect([toast.message, toast.isLasting]).toEqual([`Couldn't save "notes.md". ${said}`, false]);
    const disclosure = root.querySelector<HTMLDetailsElement>(".toast details.see-details");
    expect(disclosure?.querySelector("pre")?.textContent).toBe(
      `Tried to save ${PATH}\nHTTP 409\nPUT /home/user/workspace/notes.md`,
    );
    disclosure?.setAttribute("open", "");
    disclosure?.dispatchEvent(new Event("toggle"));
    expect(hold).toHaveBeenCalledWith(toast.id);
  });

  it("names it, and keeps the note up with the details and Report a bug where no agent can be asked", async () => {
    vi.mocked(server.readText).mockResolvedValueOnce("draft");
    const { page, root } = await openedPage(true, false);
    page.draft = "draft, changed";
    vi.mocked(server.writeFile).mockRejectedValueOnce(httpFailure(500, { detail: "No space left on device" }));

    await page.save();

    const [toast] = page.toasts.current();
    expect(toast.message).toBe('Couldn\'t save "notes.md". Try again.');
    expect(toast.isLasting).toBe(true);
    expect(toast.details).toBe(`Tried to save ${PATH}\nHTTP 500\nNo space left on device`);
    expect(page.isDirty).toBe(true);
    m.redraw.sync();
    root.querySelector<HTMLButtonElement>(".toast .report-bug")?.click();
    expect(page.reporter.reportBug).toHaveBeenCalledWith({ message: toast.message, details: toast.details });
  });

  it("asks an agent to save what a read-only folder refused, from its toast, which stays up", async () => {
    vi.mocked(server.readText).mockResolvedValueOnce("draft");
    const { page, root, host } = await openedPage(true);
    page.draft = "draft, changed";
    vi.mocked(server.writeFile).mockRejectedValueOnce(
      httpFailure(403, {
        detail: "PUT: [Errno 13]",
        user_facing_message: "You don't have permission to change this.",
        kind: "permission_denied",
      }),
    );
    await page.save();
    m.redraw.sync();
    const actions = (): (string | null)[] =>
      Array.from(root.querySelectorAll(".toast .toast-action"), (button) => button.textContent);
    // The agent stands in for the bug report, which is offered only where none can be asked.
    expect(actions()).toEqual(["Ask an agent to fix this"]);

    root.querySelector<HTMLButtonElement>(".toast .toast-action")?.click();
    await vi.waitFor(() => expect(host.askAgent).toHaveBeenCalledTimes(1));
    m.redraw.sync();

    const [task] = host.askAgent.mock.calls[0];
    expect(task).toContain("Couldn't save \"notes.md\". The folder it's in is read-only.");
    expect(task).toContain(`Tried to save ${PATH}\nHTTP 403\nPUT: [Errno 13]`);
    expect(task).toMatch(/\n\nDo what the user was trying to do if you can; otherwise [^\n]*$/);
    expect(actions()).toEqual(["Ask an agent to fix this"]);
  });
});

describe("deleting the file", () => {
  it("takes a file that is already gone as deleted, with no note", async () => {
    vi.mocked(server.readText).mockResolvedValueOnce("draft");
    const { page, root } = await openedPage(true);
    vi.spyOn(page.dialogs, "confirmDelete").mockResolvedValueOnce(true);
    vi.mocked(server.deletePath).mockRejectedValueOnce(httpFailure(404, GONE));

    await page.delete();
    m.redraw.sync();

    expect(page.toasts.current()).toEqual([]);
    expect(root.querySelector(".files-file-deleted")?.textContent).toContain('"notes.md" was deleted');
  });
});
