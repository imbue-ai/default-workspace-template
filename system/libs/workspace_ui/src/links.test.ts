/**
 * The link classifier over every kind of link the shell is handed or a chat message holds, and its agreement with the
 * Imbue Studio desktop app on which links are external.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { classifyLink, fileUrl, type LinkTarget } from "./links";

/** The URLs the Imbue Studio desktop app's ``isExternalUrl`` was asked about, with its answers, fetched from the
 *  pinned mngr commit with the embed contract (``system/scripts/fetch_mngr_assets.sh``). */
const EXTERNALITY_VECTORS_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../vendor/mngr-assets/apps/minds/electron/link-externality-vectors.json",
);

interface ExternalityVector {
  readonly url: string;
  readonly isExternal: boolean;
  readonly note: string;
}

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
    [
      "userinfo naming a local host",
      "http://localhost@evil.example/",
      { kind: "external", url: "http://localhost@evil.example/" },
    ],
    [
      "localhost with a port",
      "http://localhost:3000/x?y=1",
      { kind: "local-url", url: "http://localhost:3000/x?y=1" },
    ],
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
    ["an absolute path", "/home/user/workspace/data/q4.pdf", { kind: "unroutable" }],
    ["a relative path", "data/q4.pdf", { kind: "unroutable" }],
    ["a fragment", "#top", { kind: "unroutable" }],
    ["javascript", "javascript:alert(1)", { kind: "unroutable" }],
    ["a file URL", "file:///etc/passwd", { kind: "file", path: "/etc/passwd" }],
    [
      "an encoded file URL to a folder",
      "file:///home/user/my%20notes/",
      { kind: "file", path: "/home/user/my notes" },
    ],
    ["a file URL on localhost", "file://localhost/home/user/a.md", { kind: "file", path: "/home/user/a.md" }],
    ["a file URL to the root", "file:///", { kind: "file", path: "/" }],
    ["a file URL that does not decode", "file:///home/user/%zz", { kind: "unroutable" }],
    ["a file URL naming another machine", "file://server.example/share/a.md", { kind: "unroutable" }],
  ])("classifies %s", (_what, href, expected) => {
    expect(classifyLink(href, CHAT_HOST)).toEqual(expected);
  });
});

const SHARE_DOMAIN = "0123456789abcdef0123456789abcdef.fedcba9876543210fedcba9876543210.us1.personal-imbue.com";
const OTHER_SHARE_DOMAIN = "11111111111111111111111111111111.fedcba9876543210fedcba9876543210.us1.personal-imbue.com";

describe("classifyLink on share addresses", () => {
  it("opens one of this workspace's share addresses as that app's window from the desktop", () => {
    const url = `https://files-ab12cd34.${SHARE_DOMAIN}/home/user/?view`;

    expect(classifyLink(url, CHAT_HOST, SHARE_DOMAIN)).toEqual({
      kind: "app-address",
      label: "files-ab12cd34",
      path: "/home/user/?view",
      url,
    });
  });

  it("matches the share domain whatever its case", () => {
    const url = `https://Files-AB12CD34.${SHARE_DOMAIN.toUpperCase()}/a`;

    expect(classifyLink(url, CHAT_HOST, SHARE_DOMAIN)).toMatchObject({ kind: "app-address", label: "files-ab12cd34" });
  });

  it("opens an address on the page's own share domain as that app's window, with no share domain known", () => {
    const url = `https://files-ab12cd34.${SHARE_DOMAIN}/home/user/?view`;

    expect(classifyLink(url, `chat-ab12cd34.${SHARE_DOMAIN}`)).toMatchObject({
      kind: "app-address",
      label: "files-ab12cd34",
    });
  });

  it("leaves another workspace's share address external, since nothing tells it from any other site", () => {
    const url = `https://files-ab12cd34.${OTHER_SHARE_DOMAIN}/`;

    expect(classifyLink(url, CHAT_HOST, SHARE_DOMAIN)).toEqual({ kind: "external", url });
  });

  it("leaves a share address external when the workspace knows no share domain", () => {
    const url = `https://files-ab12cd34.${SHARE_DOMAIN}/`;

    expect(classifyLink(url, CHAT_HOST)).toEqual({ kind: "external", url });
  });

  it("calls the bare share domain unroutable", () => {
    expect(classifyLink(`https://${SHARE_DOMAIN}/`, CHAT_HOST, SHARE_DOMAIN)).toEqual({ kind: "unroutable" });
  });
});

describe("agreement with the Imbue Studio desktop app", () => {
  const vectors = JSON.parse(readFileSync(EXTERNALITY_VECTORS_PATH, "utf8")) as ExternalityVector[];

  it("has vectors to agree with", () => {
    expect(vectors.length).toBeGreaterThan(0);
  });

  it.each(vectors.map((vector) => [vector.note, vector.url, vector.isExternal] as const))(
    "calls %s external exactly when the desktop app does",
    (_note, url, isExternal) => {
      expect(classifyLink(url, CHAT_HOST).kind === "external").toBe(isExternal);
    },
  );
});

describe("fileUrl", () => {
  it.each(["/home/user/a.md", "/home/user/my notes/plan#1?.md", "/home/user/caf\u00e9/", "/"])(
    "names %s as a file URL the classifier reads back as the same path",
    (path) => {
      const expected = path.length > 1 ? path.replace(/\/+$/, "") : path;
      expect(classifyLink(fileUrl(path), CHAT_HOST)).toEqual({ kind: "file", path: expected });
    },
  );
});
