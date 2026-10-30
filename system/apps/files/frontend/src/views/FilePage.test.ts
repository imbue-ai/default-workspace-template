// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import m from "mithril";

import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import * as server from "../models/server";
import { httpFailure } from "../testing/records";
import { FilePage, FilePageModel } from "./FilePage";

vi.mock("../models/server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/server")>()),
  readText: vi.fn(),
  writeFile: vi.fn(),
}));

const PATH = "/home/user/workspace/notes.md";

function reporter(canReport: boolean): { canReportBug: () => boolean; reportBug: Mock<() => void> } {
  return { canReportBug: () => canReport, reportBug: vi.fn<() => void>() };
}

async function openedPage(canReport: boolean): Promise<{ page: FilePageModel; root: HTMLElement }> {
  const page = new FilePageModel({ path: PATH, page: "edit", isText: true }, reporter(canReport));
  await page.loadText();
  const root = mountView(() => m(FilePage, { page }));
  m.redraw.sync();
  return { page, root };
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

describe("a file whose text could not be loaded", () => {
  it("says a file that is no longer there was moved or deleted, and asks for no bug report", async () => {
    vi.mocked(server.readText).mockRejectedValueOnce(httpFailure(404, null, "read"));
    const { root } = await openedPage(true);

    expect(failureText(root)).toBe("This file is no longer there. It may have been moved or deleted.");
    expect(root.querySelector("textarea")).toBeNull();
    expect(root.querySelector(".report-bug")).toBeNull();
  });

  it("says it couldn't be opened, with the server's text behind See details and Report a bug, and opens it again", async () => {
    vi.mocked(server.readText).mockRejectedValueOnce(httpFailure(500, { detail: "Input/output error" }, "read"));
    const { page, root } = await openedPage(true);

    expect(failureText(root)).toBe("This file couldn't be opened. Try again. If this persists, report a bug.");
    expect(root.querySelector(".see-details pre")?.textContent).toBe("HTTP 500\nInput/output error");
    (root.querySelector(".report-bug") as HTMLButtonElement).click();
    expect(page.reporter.reportBug).toHaveBeenCalledTimes(1);

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

    expect(failureText(root)).toBe("This file couldn't be opened. Try again. If this persists, report a bug.");
    expect(root.querySelector(".report-bug")).toBeNull();
    expect(root.querySelector('[data-action="retry-load"]')).not.toBeNull();
  });
});

describe("a file that could not be saved", () => {
  it("names it, and keeps the note up with the details where nothing in the server's answer explains it", async () => {
    vi.mocked(server.readText).mockResolvedValueOnce("draft");
    const { page } = await openedPage(true);
    page.draft = "draft, changed";
    vi.mocked(server.writeFile).mockRejectedValueOnce(httpFailure(500, { detail: "No space left on device" }));

    await page.save();

    const [toast] = page.toasts.current();
    expect(toast.message).toBe('Couldn\'t save "notes.md". Try again. If this persists, report a bug.');
    expect(toast.isLasting).toBe(true);
    expect(toast.details).toBe("HTTP 500\nNo space left on device");
    expect(toast.actions.map((action) => action.label)).toEqual(["Report a bug"]);
    expect(page.isDirty).toBe(true);
  });
});
