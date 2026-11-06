// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import type { AddAppRequest, CheckAnswer, CheckResult, FindAnswer, FinderJob } from "../models/Store";
import type { AddAppDialogAttrs, FinderClient } from "./AddAppDialog";
import {
  isGithubLink,
  AddAppDialog,
  BODY_SCROLL_CLASS,
  FIND_POLL_MS,
  UNREVIEWED_WARNING,
  commandPreview,
  previewSlug,
} from "./AddAppDialog";

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  m.mount(root, null);
  root.remove();
});

const STREAMLIT = "streamlit hello --server.port {port} --server.address 127.0.0.1 --server.headless true";

function result(overrides: Partial<CheckResult> = {}): CheckResult {
  return {
    slug: "streamlit",
    title: "streamlit",
    source: "pypi",
    registry: "PyPI",
    package: "streamlit",
    version: "1.66.1",
    python: "3.12",
    registry_url: "https://pypi.org/project/streamlit/1.66.1/",
    summary: "A faster way to build data apps.",
    license: "Apache-2.0",
    command: STREAMLIT.split(" "),
    command_preview: ["/apps/streamlit/env/bin/streamlit", "hello", "--server.port", "8090"],
    package_count: 34,
    disk_mb: 369,
    disk_basis: "Resolved with uv for Python 3.12 on this machine: 34 packages.",
    verdict: "FITS",
    blocked_reason: "",
    setup_files: [],
    ...overrides,
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((done) => (resolve = done));
  return { promise, resolve };
}

function job(overrides: Partial<FinderJob> = {}): FinderJob {
  return {
    job_id: "job1",
    status: "WORKING",
    progress: "Downloading copyparty 1.20.26 to try it out",
    package: "copyparty",
    version: "1.20.26",
    command: "",
    explanation: "",
    attempts: [],
    has_screenshot: false,
    setup_files: [],
    started_at: "2026-11-05T19:53:42+00:00",
    ...overrides,
  };
}

function fakeFinder(polls: FinderJob[], start: FindAnswer = { kind: "started", job: job() }): FinderClient {
  return {
    start: vi.fn(async () => start),
    poll: vi.fn(async () => polls.shift() ?? null),
    cancel: vi.fn(async () => undefined),
    screenshotUrl: (jobId: string) => `/shot/${jobId}.png`,
    logUrl: (jobId: string) => `/log/${jobId}`,
  };
}

/** Mount the dialog; it opens on GitHub, so the package tests switch it to PyPI first unless told otherwise. */
function mountDialog(
  overrides: Partial<AddAppDialogAttrs> = {},
  origin: "github" | "pypi" = "pypi",
): AddAppDialogAttrs {
  const attrs: AddAppDialogAttrs = {
    room: { disk_free_mb: 30_000, memory_available_mb: 4_000 },
    finder: fakeFinder([]),
    onCheck: vi.fn(async (): Promise<CheckAnswer> => ({ kind: "checked", result: result() })),
    onAdd: vi.fn(async () => null),
    onStartWithText: vi.fn(),
    onClose: vi.fn(),
    ...overrides,
  };
  m.mount(root, { view: () => m(AddAppDialog, attrs) });
  if (origin === "pypi") {
    document.querySelector<HTMLElement>("[data-source=pypi]")!.click();
    m.redraw.sync();
  }
  return attrs;
}

function type(field: string, value: string): void {
  const input = document.querySelector<HTMLInputElement>(`[data-field=${field}]`)!;
  input.value = value;
  input.dispatchEvent(new Event("input"));
  m.redraw.sync();
}

function stage(): string | null {
  return document.querySelector("[data-dialog=add-app]")!.getAttribute("data-stage");
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  m.redraw.sync();
}

describe("commandPreview", () => {
  it("fills in sample values and splits the way the store does", () => {
    expect(commandPreview('serve "my files" -p {port} --root {data_dir}', "demo")).toEqual([
      "serve",
      "my files",
      "-p",
      "8090",
      "--root",
      "data/.apps/app-store/demo/data",
    ]);
    expect(commandPreview('serve "unclosed', "demo")).toBeNull();
    expect(previewSlug("My Notes 2")).toBe("mynotes2");
  });
});

describe("AddAppDialog", () => {
  it("scrolls its body on its own, so the buttons stay outside it and on screen", () => {
    mountDialog();
    const body = document.querySelector<HTMLElement>("[data-dialog-body]")!;
    expect(body.className).toBe(BODY_SCROLL_CLASS);
    expect(BODY_SCROLL_CLASS).toContain("overflow-y-auto");
    expect(body.querySelector(".app-store-find")).toBeNull();
    expect(document.querySelector(".app-store-find")).not.toBeNull();
  });

  it("takes the details and shows the command that will run before anything is checked", () => {
    mountDialog();
    expect(stage()).toBe("editing");
    expect(document.querySelector<HTMLButtonElement>(".app-store-find")!.disabled).toBe(true);
    type("package", "streamlit");
    type("command", STREAMLIT);
    expect(document.querySelector("[data-command-preview] code")!.textContent).toBe(
      "streamlit hello --server.port 8090 --server.address 127.0.0.1 --server.headless true",
    );
    expect(document.querySelector("[data-field=python]")).not.toBeNull();
    document.querySelector<HTMLElement>("[data-source=npm]")!.click();
    m.redraw.sync();
    expect(document.querySelector("[data-field=python]")).toBeNull();
    expect(document.querySelector<HTMLButtonElement>(".app-store-check")!.disabled).toBe(false);
  });

  it("checks, showing it is busy, then shows the pinned version, packages, estimate and unknown memory", async () => {
    const answer = deferred<CheckAnswer>();
    const attrs = mountDialog({ onCheck: vi.fn(() => answer.promise) });
    type("package", "streamlit");
    type("version", "");
    type("display-name", "Streamlit demo");
    type("command", STREAMLIT);
    document.querySelector<HTMLElement>(".app-store-check")!.click();
    m.redraw.sync();
    expect(stage()).toBe("checking");
    expect(document.querySelector<HTMLButtonElement>(".app-store-check")!.textContent).toContain("Checking");
    const sent = (attrs.onCheck as ReturnType<typeof vi.fn>).mock.calls[0][0] as AddAppRequest;
    expect(sent).toEqual({
      source: "pypi",
      package: "streamlit",
      version: "",
      display_name: "Streamlit demo",
      command: STREAMLIT,
      python: "3.12",
      setup_files: [],
    });

    answer.resolve({ kind: "checked", result: result() });
    await settle();
    expect(stage()).toBe("checked");
    expect(document.querySelector("[data-pinned]")!.textContent).toBe("streamlit 1.66.1");
    expect(document.querySelector("[data-package-count]")!.textContent).toBe("34 in all");
    expect(document.querySelector("[data-disk]")!.textContent).toBe("about 369 MB (29.3 GB free)");
    expect(document.querySelector("[data-memory]")!.textContent).toBe("unknown until it runs");
    expect(document.querySelector("[data-disk-basis]")!.textContent).toContain("Resolved with uv");
  });

  it("shows the store's refusal and stays editable", async () => {
    mountDialog({
      onCheck: vi.fn(async (): Promise<CheckAnswer> => ({ kind: "refused", reason: "Links aren't supported here." })),
    });
    type("package", "git+https://github.com/x/y");
    type("command", STREAMLIT);
    document.querySelector<HTMLElement>(".app-store-check")!.click();
    await settle();
    expect(stage()).toBe("editing");
    expect(document.querySelector("[data-refusal]")!.textContent).toBe("Links aren't supported here.");
    type("package", "streamlit");
    expect(document.querySelector("[data-refusal]")).toBeNull();
  });

  it("says when the size is unknown and blocks continuing when it won't fit", async () => {
    mountDialog({
      onCheck: vi.fn(async (): Promise<CheckAnswer> => ({
        kind: "checked",
        result: result({ disk_mb: null, package_count: null, disk_basis: "Size unknown: resolving failed." }),
      })),
    });
    type("package", "streamlit");
    type("command", STREAMLIT);
    document.querySelector<HTMLElement>(".app-store-check")!.click();
    await settle();
    expect(document.querySelector("[data-disk]")!.textContent).toBe("unknown");
    expect(document.querySelector("[data-package-count]")!.textContent).toBe("not resolved");
    expect(document.querySelector("[data-disk-basis]")!.textContent).toBe("Size unknown: resolving failed.");

    m.mount(root, null);
    mountDialog({
      onCheck: vi.fn(async (): Promise<CheckAnswer> => ({
        kind: "checked",
        result: result({ verdict: "WONT_FIT", blocked_reason: "It needs about 369 MB of disk." }),
      })),
    });
    type("package", "streamlit");
    type("command", STREAMLIT);
    document.querySelector<HTMLElement>(".app-store-check")!.click();
    await settle();
    expect(document.querySelector("[data-refusal]")!.textContent).toBe("It needs about 369 MB of disk.");
    expect(document.querySelector<HTMLButtonElement>(".app-store-continue")!.disabled).toBe(true);
  });

  it("drops the check when a detail changes, so what is confirmed is what was checked", async () => {
    mountDialog();
    type("package", "streamlit");
    type("command", STREAMLIT);
    document.querySelector<HTMLElement>(".app-store-check")!.click();
    await settle();
    expect(stage()).toBe("checked");
    type("version", "1.40.0");
    expect(stage()).toBe("editing");
    expect(document.querySelector("[data-check-result]")).toBeNull();
  });

  it("confirms with the unreviewed-code warning and the package's page, then adds the pinned version", async () => {
    const attrs = mountDialog();
    type("package", "streamlit");
    type("command", STREAMLIT);
    document.querySelector<HTMLElement>(".app-store-check")!.click();
    await settle();
    document.querySelector<HTMLElement>(".app-store-continue")!.click();
    m.redraw.sync();
    expect(stage()).toBe("confirming");
    expect(document.querySelector("[data-warning]")!.textContent).toBe(UNREVIEWED_WARNING);
    const link = document.querySelector<HTMLAnchorElement>("[data-confirmation] a")!;
    expect(link.getAttribute("href")).toBe("https://pypi.org/project/streamlit/1.66.1/");
    expect(document.querySelector("[data-confirmation]")!.textContent).toContain("under Apache-2.0");

    document.querySelector<HTMLElement>(".app-store-confirm-add")!.click();
    await settle();
    const sent = (attrs.onAdd as ReturnType<typeof vi.fn>).mock.calls[0][0] as AddAppRequest;
    expect(sent.version).toBe("1.66.1");
    expect(attrs.onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps the confirmation open with the reason when adding is refused", async () => {
    const attrs = mountDialog({ onAdd: vi.fn(async () => "Streamlit is already installed or being worked on.") });
    type("package", "streamlit");
    type("command", STREAMLIT);
    document.querySelector<HTMLElement>(".app-store-check")!.click();
    await settle();
    document.querySelector<HTMLElement>(".app-store-continue")!.click();
    m.redraw.sync();
    document.querySelector<HTMLElement>(".app-store-confirm-add")!.click();
    await settle();
    expect(stage()).toBe("confirming");
    expect(document.querySelector("[data-refusal]")!.textContent).toBe(
      "Streamlit is already installed or being worked on.",
    );
    expect(attrs.onClose).not.toHaveBeenCalled();
    Array.from(document.querySelectorAll<HTMLElement>(".modal-actions button"))
      .find((button) => button.textContent === "Back")!
      .click();
    m.redraw.sync();
    expect(stage()).toBe("checked");
  });
  describe("working out the start command", () => {
    beforeEach(() => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }));
    afterEach(() => vi.useRealTimers());

    async function flush(): Promise<void> {
      await vi.advanceTimersByTimeAsync(0);
      m.redraw.sync();
    }

    async function poll(): Promise<void> {
      await vi.advanceTimersByTimeAsync(FIND_POLL_MS);
      await flush();
    }

    function click(selector: string): void {
      document.querySelector<HTMLButtonElement>(selector)!.click();
    }

    it("offers to work it out while the command is empty, and checks once one is typed", () => {
      mountDialog();
      type("package", "copyparty");
      expect(document.querySelector(".app-store-find")).not.toBeNull();
      expect(document.querySelector(".app-store-check")).toBeNull();
      type("command", "copyparty -p {port} -i 127.0.0.1");
      expect(document.querySelector(".app-store-find")).toBeNull();
      expect(document.querySelector(".app-store-check")).not.toBeNull();
    });

    it("shows progress and each try, then the screenshot, and checks the found command when it looks right", async () => {
      const found = job({
        status: "FOUND",
        progress: "",
        command: "copyparty -i 127.0.0.1 -p {port} -v {data_dir}::rw",
        explanation: "A file server you open in your browser.",
        attempts: [
          { command: "copyparty -p {port}", outcome: "It broke one of the store's rules." },
          { command: "copyparty -i 127.0.0.1 -p {port} -v {data_dir}::rw", outcome: "It started and showed a page." },
        ],
        has_screenshot: true,
      });
      const finder = fakeFinder([job({ progress: "Test-launching it", attempts: [found.attempts[0]] }), found]);
      const attrs = mountDialog({ finder });
      type("package", "copyparty");
      click(".app-store-find");
      await flush();
      expect(stage()).toBe("finding");
      expect(finder.start).toHaveBeenCalledWith(expect.objectContaining({ package: "copyparty", command: "" }));
      expect(document.querySelector("[data-progress]")!.textContent).toContain("Downloading copyparty");
      expect(document.querySelector<HTMLInputElement>("[data-field=package]")!.disabled).toBe(true);

      await poll();
      expect(document.querySelector("[data-progress]")!.textContent).toContain("Test-launching it");
      expect(document.querySelector("[data-attempts]")!.textContent).toContain("broke one of the store's rules");

      await poll();
      expect(stage()).toBe("found");
      expect(document.querySelector<HTMLImageElement>("[data-screenshot]")!.getAttribute("src")).toBe(
        "/shot/job1.png",
      );
      expect(document.querySelector("[data-explanation]")!.textContent).toBe(
        "A file server you open in your browser.",
      );
      expect(document.querySelector("[data-found-command]")!.textContent).toBe(found.command);
      expect(document.querySelector<HTMLAnchorElement>("[data-finder-log]")!.getAttribute("href")).toBe("/log/job1");

      click(".app-store-use-found");
      await flush();
      expect(attrs.onCheck).toHaveBeenCalledWith(
        expect.objectContaining({ package: "copyparty", command: found.command, version: "1.20.26" }),
      );
      expect(stage()).toBe("checked");
    });

    it("leaves the found command in the field to change when it does not look right", async () => {
      const found = job({ status: "FOUND", command: "copyparty -i 127.0.0.1 -p {port}" });
      mountDialog({ finder: fakeFinder([found]) });
      type("package", "copyparty");
      click(".app-store-find");
      await flush();
      await poll();
      click(".app-store-type-myself");
      m.redraw.sync();
      expect(stage()).toBe("editing");
      expect(document.querySelector<HTMLInputElement>("[data-field=command]")!.value).toBe(found.command);
    });

    it.each([
      ["NOT_AN_APP", "This isn't an app with a window", "Try another package"],
      ["NEEDS_SETUP", "This app needs setting up first", "Try another package"],
      ["FAILED", "Couldn't get this app to start", "Type the command myself"],
    ] as const)("explains a %s ending in plain words", async (status, heading, back) => {
      const ended = job({
        status,
        explanation: "It needs an account first.",
        attempts: [{ command: "x -p {port}", outcome: "It stopped right away (exit status 1)." }],
      });
      mountDialog({ finder: fakeFinder([ended]) });
      type("package", "copyparty");
      click(".app-store-find");
      await flush();
      await poll();
      expect(stage()).toBe("find-ended");
      const panel = document.querySelector(`[data-find-ended=${status}]`)!;
      expect(panel.textContent).toContain(heading);
      expect(panel.textContent).toContain("It needs an account first.");
      expect(panel.textContent).toContain("It stopped right away");
      expect(panel.textContent).toContain("Nothing was left installed.");
      expect(document.querySelector(".app-store-back-to-form")!.textContent).toBe(back);
      click(".app-store-back-to-form");
      m.redraw.sync();
      expect(stage()).toBe("editing");
    });

    it("shows the store's refusal without starting", async () => {
      mountDialog({ finder: fakeFinder([], { kind: "refused", reason: "TiddlyWiki is already in the store." }) });
      type("package", "tiddlywiki");
      click(".app-store-find");
      await flush();
      expect(stage()).toBe("editing");
      expect(document.querySelector("[data-refusal]")!.textContent).toBe("TiddlyWiki is already in the store.");
    });

    it("shows the settings files a found command starts with, and sends them when it looks right", async () => {
      const files = [{ path: "config.yaml", content: "web:\n  title: Mine\n" }];
      const found = job({ status: "FOUND", command: "verdaccio --config {data_dir}/config.yaml", setup_files: files });
      const attrs = mountDialog({ finder: fakeFinder([found]) });
      type("package", "verdaccio");
      click(".app-store-find");
      await flush();
      await poll();
      const shown = document.querySelector("[data-found] [data-setup-file='config.yaml']")!;
      expect(shown.textContent).toContain("title: Mine");
      click(".app-store-use-found");
      await flush();
      expect(attrs.onCheck).toHaveBeenCalledWith(expect.objectContaining({ setup_files: files }));
    });

    it("keeps the files when the command is changed by hand, drops them on request or for another package", async () => {
      const files = [{ path: "config.yaml", content: "a: 1\n" }];
      const found = job({ status: "FOUND", command: "verdaccio --config {data_dir}/config.yaml", setup_files: files });
      const attrs = mountDialog({ finder: fakeFinder([found]) });
      type("package", "verdaccio");
      click(".app-store-find");
      await flush();
      await poll();
      click(".app-store-type-myself");
      m.redraw.sync();
      expect(document.querySelector("[data-form-setup-files] [data-setup-file='config.yaml']")).not.toBeNull();
      click("[data-remove-setup-files]");
      m.redraw.sync();
      expect(document.querySelector("[data-form-setup-files]")).toBeNull();
      click(".app-store-check");
      await flush();
      expect(attrs.onCheck).toHaveBeenCalledWith(expect.objectContaining({ setup_files: [] }));
    });

    it("stops the job when asked, and when the dialog closes mid-way", async () => {
      const finder = fakeFinder([job(), job()]);
      mountDialog({ finder });
      type("package", "copyparty");
      click(".app-store-find");
      await flush();
      click(".app-store-stop-find");
      m.redraw.sync();
      expect(finder.cancel).toHaveBeenCalledWith("job1");
      expect(stage()).toBe("editing");

      click(".app-store-find");
      await flush();
      m.mount(root, null);
      expect(finder.cancel).toHaveBeenCalledTimes(2);
    });
  });
});

describe("adding from GitHub", () => {
  it("is the first choice and the one the dialog opens on", () => {
    mountDialog({}, "github");
    const sources = [...document.querySelectorAll<HTMLElement>("[data-source]")].map((segment) => segment.textContent);
    expect(sources).toEqual(["GitHub", "Python (PyPI)", "Node (npm)"]);
    expect(document.querySelector("[data-field=github-link]")).not.toBeNull();
    expect(document.querySelector("[data-field=package]")).toBeNull();
  });

  it("opens a chat that works out the install, and closes", () => {
    const attrs = mountDialog({}, "github");
    const button = document.querySelector<HTMLButtonElement>(".app-store-github-chat")!;
    expect(button.disabled).toBe(true);
    type("github-link", "https://gitlab.com/someone/thing");
    expect(document.querySelector("[data-link-problem]")).not.toBeNull();
    expect(button.disabled).toBe(true);
    type("github-link", "  https://github.com/coleifer/sqlite-web  ");
    expect(document.querySelector("[data-link-problem]")).toBeNull();
    document.querySelector<HTMLButtonElement>(".app-store-github-chat")!.click();
    expect(attrs.onStartWithText).toHaveBeenCalledWith("/add-repo-to-store https://github.com/coleifer/sqlite-web");
    expect(attrs.onClose).toHaveBeenCalledOnce();
  });

  it("switches to the link form from a package that was already checked", async () => {
    mountDialog();
    type("package", "streamlit");
    type("command", STREAMLIT);
    document.querySelector<HTMLElement>(".app-store-check")!.click();
    await settle();
    expect(stage()).toBe("checked");
    document.querySelector<HTMLElement>("[data-source=github]")!.click();
    m.redraw.sync();
    expect(document.querySelector("[data-field=github-link]")).not.toBeNull();
    expect(document.querySelector(".app-store-continue")).toBeNull();
    expect(stage()).toBe("editing");
  });

  it("knows a link to a branch, tag, release or commit", () => {
    for (const link of [
      "https://github.com/a/b",
      "github.com/a/b.git",
      "https://github.com/a/b/tree/v1.2",
      "https://github.com/a/b/releases/tag/v1.2",
      "https://github.com/a/b/commit/d1cc495a966cafaed3e50dad73cd88e790031704",
    ]) {
      expect(isGithubLink(link)).toBe(true);
    }
    for (const link of ["https://github.com/a", "a/b", "https://github.com/a/b c"]) {
      expect(isGithubLink(link)).toBe(false);
    }
  });
});
