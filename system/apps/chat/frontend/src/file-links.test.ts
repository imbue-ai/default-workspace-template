// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { workspaceFilePath, prepareFileLinks, handleFileLinkClick } from "./file-links";

const openAppPath = vi.hoisted(() => vi.fn());
vi.mock("@imbue/workspace-ui/src/app_contract", () => ({
  connectToShell: () => ({ isFramed: true, openAppPath }),
}));

describe("workspace file links", () => {
  it.each([
    [".agents/skills/assist/SKILL.md", "/.agents/skills/assist/SKILL.md?view"],
    ["/home/user/workspace/data/report.md:12", "/data/report.md?view"],
    ["/home/user/workspace/data/a%23b%3Fc.txt", "/data/a%23b%3Fc.txt?view"],
    ["data/reports/", "/data/reports/"],
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
