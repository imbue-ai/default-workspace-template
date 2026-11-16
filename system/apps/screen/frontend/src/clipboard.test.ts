import { describe, expect, it } from "vitest";
import { ClipboardBridge, nekoRouteWithToken, remoteHtmlOf } from "./clipboard";
import type { ClipboardTransport, LocalClipboard } from "./clipboard";

interface Harness {
  bridge: ClipboardBridge;
  remote: { text: string; html: string; image: Blob | null };
  local: { text: string; html: string; image: Blob | null; isFocused: boolean };
  log: string[];
}

/** The fake Screen models neko over ``xclip``: a text write owns the X clipboard with one target, and an xclip
 *  owner answers every target with its payload, so neko reads the write back with the HTML equal to the text. A
 *  copy made by a program on the Screen is staged by setting ``remote`` directly. ``isRemoteRefusing`` makes every
 *  write to the Screen fail, as the service's image route does for a session that does not hold control. */
function build(isRemoteRefusing = false): Harness {
  const remote = { text: "", html: "", image: null as Blob | null };
  const local = { text: "", html: "", image: null as Blob | null, isFocused: true };
  const log: string[] = [];
  const transport: ClipboardTransport = {
    async readRemoteText() {
      return { text: remote.text, html: remote.html };
    },
    async readRemoteImage() {
      return remote.image;
    },
    async writeRemoteText(text) {
      if (isRemoteRefusing) return false;
      remote.text = text;
      remote.html = text;
      log.push(`remote-text:${text}`);
      return true;
    },
    async writeRemoteImage(image) {
      if (isRemoteRefusing) return false;
      remote.image = image;
      log.push(`remote-image:${image.size}`);
      return true;
    },
  };
  const localClipboard: LocalClipboard = {
    async write(items) {
      if (!local.isFocused) throw new Error("not focused");
      if (items.text !== undefined) local.text = items.text;
      if (items.html !== undefined) local.html = items.html;
      if (items.image) local.image = items.image;
      log.push(`local-write:${items.text ?? ""}${items.image ? "[image]" : ""}`);
    },
    async read() {
      return { text: local.text, html: local.html, image: local.image };
    },
  };
  const bridge = new ClipboardBridge(transport, localClipboard, (message) => log.push(`status:${message}`));
  bridge.isActive = true;
  return { bridge, remote, local, log };
}

/** A paste event's data carrying one plain-text string. */
function textPaste(text: string): DataTransfer {
  return { items: [], getData: (type: string) => (type === "text/plain" ? text : "") } as unknown as DataTransfer;
}

/** A paste event's data carrying plain text and an HTML flavor, as a copy from an editor or a terminal does. */
function richPaste(text: string, html: string): DataTransfer {
  return {
    items: [],
    getData: (type: string) => (type === "text/plain" ? text : type === "text/html" ? html : ""),
  } as unknown as DataTransfer;
}

describe("ClipboardBridge", () => {
  it("brings remote text and html to the local clipboard on neko's update", async () => {
    const { bridge, remote, local } = build();
    remote.text = "hello";
    remote.html = "<b>hello</b>";
    await bridge.onRemoteTextUpdated();
    expect(local.text).toBe("hello");
    expect(local.html).toBe("<b>hello</b>");
  });

  it("writes no html locally when neko reports the plain text as the html", async () => {
    const { bridge, remote, local } = build();
    remote.text = "ls -la";
    remote.html = "ls -la";
    await bridge.onRemoteTextUpdated();
    expect(local.text).toBe("ls -la");
    expect(local.html).toBe("");
  });

  it("fetches and writes an image when the service says one is there", async () => {
    const { bridge, remote, local } = build();
    remote.image = new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
    await bridge.onRemoteImageUpdated();
    expect(local.image).toBe(remote.image);
  });

  it("leaves the local clipboard unread on focus: only a paste crosses", () => {
    const { bridge, remote, local, log } = build();
    local.text = "from laptop";
    bridge.onFocus();
    expect(remote.text).toBe("");
    expect(log).toEqual([]);
  });

  it("sends only the plain text of a rich paste", async () => {
    const { bridge, remote, log } = build();
    expect(await bridge.onPaste(richPaste("ls -la", "<pre>ls -la</pre>"))).toBe(true);
    expect(remote.text).toBe("ls -la");
    expect(log).toEqual(["remote-text:ls -la"]);
  });

  it("reports a paste that carries no text, and sends nothing", async () => {
    const { bridge, log } = build();
    expect(await bridge.onPaste(richPaste("", "<img src='x'>"))).toBe(false);
    expect(log).toEqual(["status:There is no text to paste on the Screen"]);
  });

  it("treats neko's announcement of the viewer's own write as an echo, not as a copy from the Screen", async () => {
    const { bridge, local, log } = build();
    local.text = "python";
    local.html = "<pre>python</pre>";
    expect(await bridge.onPaste(richPaste("python", "<pre>python</pre>"))).toBe(true);
    await bridge.onRemoteTextUpdated();
    await bridge.onRemoteTextUpdated();
    expect(local.html).toBe("<pre>python</pre>");
    expect(log).toEqual(["remote-text:python"]);
  });

  it("sends a pasted text to the Screen, once per content", async () => {
    const { bridge, remote, log } = build();
    const paste = textPaste("from laptop");
    expect(await bridge.onPaste(paste)).toBe(true);
    expect(await bridge.onPaste(paste)).toBe(true);
    expect(remote.text).toBe("from laptop");
    expect(log.filter((entry) => entry.startsWith("remote-text"))).toHaveLength(1);
  });

  it("sends a text again once the Screen's clipboard has moved on", async () => {
    const { bridge, remote, log } = build();
    expect(await bridge.onPaste(textPaste("first"))).toBe(true);
    remote.text = "copied remotely";
    remote.html = "<b>copied remotely</b>";
    await bridge.onRemoteTextUpdated();
    expect(await bridge.onPaste(textPaste("first"))).toBe(true);
    expect(log.filter((entry) => entry.startsWith("remote-text"))).toEqual(["remote-text:first", "remote-text:first"]);
  });

  it("does not echo what it just received back to the Screen, and leaves the Screen's rich copy in place", async () => {
    const { bridge, remote, local, log } = build();
    remote.text = "copied remotely";
    remote.html = "<b>copied remotely</b>";
    await bridge.onRemoteTextUpdated();
    expect(local.text).toBe("copied remotely");
    expect(await bridge.onPaste(richPaste("copied remotely", "<meta charset='utf-8'><b>copied remotely</b>"))).toBe(
      true,
    );
    expect(log.filter((entry) => entry.startsWith("remote-text"))).toHaveLength(0);
    expect(remote.html).toBe("<b>copied remotely</b>");
  });

  it("prefers an image on paste and posts it to the service", async () => {
    const { bridge, remote } = build();
    const file = new File([new Uint8Array(16)], "shot.png", { type: "image/png" });
    const data = {
      items: [{ kind: "file", type: "image/png", getAsFile: () => file }],
      getData: (type: string) => (type === "text/plain" ? "ignored" : ""),
    } as unknown as DataTransfer;
    expect(await bridge.onPaste(data)).toBe(true);
    expect(remote.image).toBe(file);
    expect(remote.text).toBe("");
  });

  it("does not bring the viewer's own image back when the service announces it", async () => {
    const { bridge, local, log } = build();
    const file = new File([new Uint8Array(16)], "shot.png", { type: "image/png" });
    const data = {
      items: [{ kind: "file", type: "image/png", getAsFile: () => file }],
      getData: () => "",
    } as unknown as DataTransfer;
    expect(await bridge.onPaste(data)).toBe(true);
    await bridge.onRemoteImageUpdated();
    expect(local.image).toBeNull();
    expect(log).toEqual(["remote-image:16"]);
  });

  it("reports a paste the Screen refused", async () => {
    const { bridge, log } = build(true);
    const file = new File([new Uint8Array(16)], "shot.png", { type: "image/png" });
    const imagePaste = {
      items: [{ kind: "file", type: "image/png", getAsFile: () => file }],
      getData: () => "",
    } as unknown as DataTransfer;
    expect(await bridge.onPaste(imagePaste)).toBe(false);
    expect(log).toContain("status:The image could not be pasted on the Screen");
    expect(await bridge.onPaste(textPaste("hello"))).toBe(false);
    expect(log).toContain("status:The text could not be pasted on the Screen");
  });

  it("does nothing while this viewer does not control", async () => {
    const { bridge, remote, local } = build();
    bridge.isActive = false;
    remote.text = "secret";
    await bridge.onRemoteTextUpdated();
    expect(local.text).toBe("");
    expect(await bridge.onPaste(textPaste("mine"))).toBe(false);
    expect(remote.text).toBe("secret");
  });

  it("keeps a failed local write for the next focus", async () => {
    const { bridge, remote, local, log } = build();
    local.isFocused = false;
    remote.text = "later";
    await bridge.onRemoteTextUpdated();
    expect(local.text).toBe("");
    expect(log.some((entry) => entry.startsWith("status:Copied; click"))).toBe(true);
    local.isFocused = true;
    bridge.onFocus();
    await Promise.resolve();
    expect(local.text).toBe("later");
  });
});

describe("remoteHtmlOf", () => {
  it("keeps html that differs from the text and drops html that repeats it", () => {
    expect(remoteHtmlOf({ text: "a", html: "<b>a</b>" })).toBe("<b>a</b>");
    expect(remoteHtmlOf({ text: "a", html: "a" })).toBe("");
    expect(remoteHtmlOf({ text: "a", html: "" })).toBe("");
  });
});

describe("nekoRouteWithToken", () => {
  it("carries the session token in the query string, encoded, and leaves a token-less route alone", () => {
    expect(nekoRouteWithToken("/neko/api/room/clipboard", "abc/+=")).toBe(
      "/neko/api/room/clipboard?token=abc%2F%2B%3D",
    );
    expect(nekoRouteWithToken("/neko/api/room/clipboard", null)).toBe("/neko/api/room/clipboard");
  });
});
