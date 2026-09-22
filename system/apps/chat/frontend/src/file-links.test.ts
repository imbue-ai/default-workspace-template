// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { MarkdownContent } from "./markdown";
import { openImageLightbox } from "./lightbox";
import { workspaceFilePath, prepareFileLinks, handleFileLinkClick } from "./file-links";

vi.mock("./lightbox", () => ({ openImageLightbox: vi.fn() }));

const openAppPath = vi.hoisted(() => vi.fn());
vi.mock("@imbue/workspace-ui/src/app_contract", () => ({
  connectToShell: () => ({ isFramed: true, openAppPath }),
}));

beforeEach(() => {
  openAppPath.mockClear();
  document.head.innerHTML = "";
});

afterEach(() => vi.unstubAllGlobals());

describe("workspace file links", () => {
  it.each([
    [".agents/skills/assist/SKILL.md", "/.agents/skills/assist/SKILL.md?view"],
    ["/home/user/workspace/data/report.md:12", "/data/report.md?view"],
    ["/home/user/workspace/data/a%23b%3Fc.txt", "/data/a%23b%3Fc.txt?view"],
    ["data/reports/", "/data/reports/"],
    ["./docs/guide.md:12:4#heading", "/docs/guide.md?view"],
    ["docs/a%20b%25.txt", "/docs/a%20b%25.txt?view"],
    ["/data", "/data?view"],
    ["/home/user/workspace", "/?view"],
    ["/home/user/workspace/", "/"],
    ["docs/../data/report.md", "/data/report.md?view"],
    ["data/missing.txt", "/data/missing.txt?view"],
    [" https://example.com ", null],
    ["?query=one", null],
    ["https://example.com", null],
    ["//example.com/file", null],
    ["mailto:reader@example.com", null],
    ["#heading", null],
    ["/api/uploads/file", null],
    ["/tmp/report.md", null],
    ["/home/user/workspace-other/report.md", null],
    ["%invalid", null],
  ])("routes %s without replacing the chat", (href, expected) => {
    expect(workspaceFilePath(href, ["/home/user/workspace"])).toBe(expected);
  });

  it("opens a nested link label in a Files window and cancels browser navigation", () => {
    document.head.innerHTML = '<meta name="chat-files-label" content="files-example">';
    const container = document.createElement("div");
    container.innerHTML = '<a href=".agents/skills/assist/SKILL.md" target="_self"><strong>Instructions</strong></a>';
    prepareFileLinks(container);
    container.addEventListener("click", handleFileLinkClick);
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    container.querySelector("strong")!.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(openAppPath).toHaveBeenCalledWith("files", "/.agents/skills/assist/SKILL.md?view", "focus");
  });

  it("forces external and missing file links into separate tabs, including authored targets", () => {
    const container = document.createElement("div");
    container.innerHTML = '<a href="https://example.com" target="_top">Web</a><a href="/missing/file">Missing</a>';
    prepareFileLinks(container);
    for (const anchor of container.querySelectorAll("a")) {
      expect(anchor.target).toBe("_blank");
      expect(anchor.rel).toBe("noopener noreferrer");
    }
  });
});

describe("browser link behavior", () => {
  it.each([{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }])(
    "leaves modified clicks to the native separate-tab link (%j)",
    (modifiers) => {
      const container = document.createElement("div");
      container.innerHTML = '<a href="docs/readme.md">Readme</a>';
      prepareFileLinks(container);
      container.addEventListener("click", handleFileLinkClick);
      const event = new MouseEvent("click", {
        bubbles: true,
        cancelable: true,
        ...modifiers,
      });
      container.querySelector("a")!.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(false);
      expect(openAppPath).not.toHaveBeenCalled();
      expect(container.querySelector("a")!.target).toBe("_blank");
    },
  );

  it("ignores an authored routing attribute on an external link", () => {
    const container = document.createElement("div");
    container.innerHTML = '<a href="https://example.com" data-workspace-file="/wrong?view" target="_self">Web</a>';
    prepareFileLinks(container);
    container.addEventListener("click", handleFileLinkClick);
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    container.querySelector("a")!.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(openAppPath).not.toHaveBeenCalled();
    expect(container.querySelector("a")!.dataset.workspaceFile).toBeUndefined();
  });

  it("keeps a usable native href on a loopback preview", () => {
    document.head.innerHTML = '<meta name="chat-files-label" content="files-example">';
    const container = document.createElement("div");
    container.innerHTML = '<a href="docs/readme.md">Readme</a>';
    prepareFileLinks(container);
    expect(container.querySelector("a")!.getAttribute("href")).toBe("docs/readme.md");
  });
});

it.each([
  [
    "http://chat-abc.host-aabb.localhost:8421/",
    "http://files-example.host-aabb.localhost:8421/docs/a%23b%3Fc.md?view",
  ],
  [
    "https://chat-abc.0123456789abcdef0123456789abcdef.user.region.example/",
    "https://files-example.0123456789abcdef0123456789abcdef.user.region.example/docs/a%23b%3Fc.md?view",
  ],
])("rewrites the native file href on deployed hosts (%s)", (url, expected) => {
  vi.stubGlobal("window", { location: new URL(url) });
  document.head.innerHTML = '<meta name="chat-files-label" content="files-example">';
  const container = document.createElement("div");
  container.innerHTML = '<a href="docs/a%23b%3Fc.md">Readme</a>';
  prepareFileLinks(container);
  expect(container.querySelector("a")!.href).toBe(expected);
});

it("prepares links after markdown updates and keeps linked images in the lightbox", () => {
  const container = document.createElement("div");
  document.body.appendChild(container);
  m.render(container, m(MarkdownContent, { content: "[Readme](docs/readme.md)" }));
  expect(container.querySelector("a")!.dataset.workspaceFile).toBe("/docs/readme.md?view");
  m.render(
    container,
    m(MarkdownContent, {
      content: '<a href="https://example.com" target="_top">Web</a> [![Chart](/chart.png)](docs/readme.md)',
    }),
  );
  expect(container.querySelector("a")!.target).toBe("_blank");
  expect(container.querySelector("a")!.dataset.workspaceFile).toBeUndefined();
  const image = container.querySelector("img")!;
  image.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  expect(openImageLightbox).toHaveBeenCalledWith(image.src, "Chart");
  expect(openAppPath).not.toHaveBeenCalled();
  m.render(container, []);
  container.remove();
});
