// @vitest-environment jsdom
/**
 * The link classifier over every kind of link a chat or an app page can hold, the router's action for each (framed
 * and not), and the delegated click routing: a plain, modified, or middle click is routed and never navigates.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { classifyLink, installLinkRouting, routeLink, type LinkRoutingContext, type LinkTarget } from "./links";

const COORDINATE = "host-0123456789abcdef0123456789abcdef.localhost:8421";
const OTHER_COORDINATE = "host-fedcba9876543210fedcba9876543210.localhost:8421";
const CHAT_HOST = `chat-ab12cd34.${COORDINATE}`;

describe("classifyLink", () => {
  it.each<[string, string, LinkTarget]>([
    ["a web link", "https://example.com/a?b=1", { kind: "external", url: "https://example.com/a?b=1" }],
    ["an uppercase web link", "HTTP://Example.COM/A", { kind: "external", url: "http://example.com/A" }],
    ["mailto", "mailto:someone@example.com", { kind: "external", url: "mailto:someone@example.com" }],
    ["tel", "tel:+15551234567", { kind: "external", url: "tel:+15551234567" }],
    ["a malformed web link", "http://exa mple.com/x", { kind: "external", url: "http://exa mple.com/x" }],
    ["userinfo naming a local host", "http://localhost@evil.example/", { kind: "external", url: "http://localhost@evil.example/" }],
    ["localhost with a port", "http://localhost:3000/x?y=1", { kind: "local-url", url: "http://localhost:3000/x?y=1" }],
    ["uppercase localhost", "HTTP://LOCALHOST:3000", { kind: "local-url", url: "http://localhost:3000/" }],
    ["127.0.0.1", "http://127.0.0.1:8080/", { kind: "local-url", url: "http://127.0.0.1:8080/" }],
    ["IPv6 loopback", "http://[::1]:5000/app", { kind: "local-url", url: "http://[::1]:5000/app" }],
    ["a *.localhost host", "https://dev.localhost:5173/", { kind: "local-url", url: "https://dev.localhost:5173/" }],
    [
      "one of this workspace's app addresses",
      `http://files-ab12cd34.${COORDINATE}/home/user/?view`,
      {
        kind: "app-address",
        label: "files-ab12cd34",
        path: "/home/user/?view",
        url: `http://files-ab12cd34.${COORDINATE}/home/user/?view`,
      },
    ],
    [
      "another workspace's app address",
      `http://files-ab12cd34.${OTHER_COORDINATE}/`,
      { kind: "other-workspace", url: `http://files-ab12cd34.${OTHER_COORDINATE}/` },
    ],
    ["this workspace's bare address", `http://${COORDINATE}/`, { kind: "unroutable" }],
    ["an absolute path", "/home/user/workspace/data/q4.pdf", { kind: "file", path: "/home/user/workspace/data/q4.pdf" }],
    [
      "an encoded path with the chat's cache key",
      "/home/user/my%20notes/plan%23.md?requested_at=2026-09-29",
      { kind: "file", path: "/home/user/my notes/plan#.md" },
    ],
    ["a folder with a trailing slash", "/home/user/data/", { kind: "file", path: "/home/user/data" }],
    ["the root", "/", { kind: "file", path: "/" }],
    ["a protocol-relative host", "//evil.example/x", { kind: "unroutable" }],
    ["a backslashed host", "/\\evil.example/x", { kind: "unroutable" }],
    ["a path that does not decode", "/home/user/%zz", { kind: "unroutable" }],
    ["a relative path", "data/q4.pdf", { kind: "unroutable" }],
    ["a fragment", "#top", { kind: "unroutable" }],
    ["javascript", "javascript:alert(1)", { kind: "unroutable" }],
    ["a file URL", "file:///etc/passwd", { kind: "unroutable" }],
  ])("classifies %s", (_what, href, expected) => {
    expect(classifyLink(href, CHAT_HOST)).toEqual(expected);
  });
});

interface RecordingContext extends LinkRoutingContext {
  readonly calls: unknown[][];
}

function recordingContext(isFramed: boolean): RecordingContext {
  const calls: unknown[][] = [];
  return {
    calls,
    isFramed,
    pageHost: CHAT_HOST,
    openPath: (path, ifPresent) => calls.push(["openPath", path, ifPresent]),
    sendMessage: (type, fields) => calls.push(["sendMessage", type, fields]),
    openInNewTab: (url) => calls.push(["openInNewTab", url]),
    download: (path) => calls.push(["download", path]),
  };
}

describe("routeLink", () => {
  it.each<[string, string, unknown[]]>([
    ["a file as open:file", "/home/user/a%20b.md", ["sendMessage", "open:file", { path: "/home/user/a b.md" }]],
    ["a local URL as open:url", "http://localhost:3000/", ["sendMessage", "open:url", { url: "http://localhost:3000/" }]],
    ["the page's own app address in place", `http://chat-ab12cd34.${COORDINATE}/?chat=agent-1`, ["openPath", "/?chat=agent-1", "focus"]],
    ["another app's address as a popup", `http://files-ab12cd34.${COORDINATE}/`, ["openInNewTab", `http://files-ab12cd34.${COORDINATE}/`]],
    ["another workspace's address as a popup", `http://x-ab12cd34.${OTHER_COORDINATE}/`, ["openInNewTab", `http://x-ab12cd34.${OTHER_COORDINATE}/`]],
    ["an external link to the browser", "https://example.com/", ["openInNewTab", "https://example.com/"]],
  ])("framed, routes %s", (_what, href, expected) => {
    const context = recordingContext(true);
    expect(routeLink(href, context)).toBe(true);
    expect(context.calls).toEqual([expected]);
  });

  it.each<[string, string, unknown[]]>([
    ["a file as a download of the link as written", "/home/user/a.md?requested_at=1", ["download", "/home/user/a.md?requested_at=1"]],
    ["a local URL in a new tab", "http://localhost:3000/", ["openInNewTab", "http://localhost:3000/"]],
    ["the page's own app address in a new tab", `http://chat-ab12cd34.${COORDINATE}/`, ["openInNewTab", `http://chat-ab12cd34.${COORDINATE}/`]],
  ])("unframed, falls back for %s", (_what, href, expected) => {
    const context = recordingContext(false);
    expect(routeLink(href, context)).toBe(true);
    expect(context.calls).toEqual([expected]);
  });

  it("leaves an unroutable link alone", () => {
    const context = recordingContext(true);
    expect(routeLink("data/q4.pdf", context)).toBe(false);
    expect(context.calls).toEqual([]);
  });
});

describe("installLinkRouting", () => {
  let uninstall: (() => void) | null = null;

  afterEach(() => {
    uninstall?.();
    uninstall = null;
    document.body.innerHTML = "";
  });

  function install(context: LinkRoutingContext): { routed: HTMLAnchorElement; other: HTMLAnchorElement } {
    document.body.innerHTML = `<div id="root"><div class="markdown-content">
      <a id="routed" href="/home/user/plan.md"><b id="inner">plan</b></a>
      <a id="relative" href="data/x.md">x</a>
    </div><a id="other" href="/api/logout">outside</a></div>`;
    const root = document.getElementById("root") as Element;
    uninstall = installLinkRouting(root, ".markdown-content a[href]", context);
    return {
      routed: document.getElementById("routed") as HTMLAnchorElement,
      other: document.getElementById("other") as HTMLAnchorElement,
    };
  }

  function click(element: Element, init: MouseEventInit = {}, type = "click"): MouseEvent {
    const event = new MouseEvent(type, { bubbles: true, cancelable: true, ...init });
    element.dispatchEvent(event);
    return event;
  }

  it("routes a plain, a modified, and a middle click alike and cancels each navigation", () => {
    const context = recordingContext(true);
    const { routed } = install(context);
    const inner = document.getElementById("inner") as Element;

    const plain = click(inner);
    const modified = click(routed, { metaKey: true, ctrlKey: true });
    const middle = click(routed, { button: 1 }, "auxclick");
    const right = click(routed, { button: 2 }, "auxclick");

    expect([plain.defaultPrevented, modified.defaultPrevented, middle.defaultPrevented]).toEqual([true, true, true]);
    expect(right.defaultPrevented).toBe(false);
    const opened = ["sendMessage", "open:file", { path: "/home/user/plan.md" }];
    expect(context.calls).toEqual([opened, opened, opened]);
  });

  it("leaves unroutable links, links outside the selector, and clicks a page already handled to the page", () => {
    const context = recordingContext(true);
    const { routed, other } = install(context);
    routed.addEventListener("click", (event) => event.preventDefault(), { capture: true });

    const handled = click(routed);
    const relative = click(document.getElementById("relative") as Element);
    const outside = click(other);

    expect(handled.defaultPrevented).toBe(true);
    expect([relative.defaultPrevented, outside.defaultPrevented]).toEqual([false, false]);
    expect(context.calls).toEqual([]);
  });

  it("stops routing once uninstalled", () => {
    const context = recordingContext(true);
    const { routed } = install(context);
    uninstall?.();
    uninstall = null;

    expect(click(routed).defaultPrevented).toBe(false);
    expect(context.calls).toEqual([]);
  });
});
