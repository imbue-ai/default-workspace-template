import { describe, expect, it } from "vitest";
import { ClipboardBridge } from "./clipboard";
import type { ClipboardTransport, LocalClipboard } from "./clipboard";

interface Harness {
  bridge: ClipboardBridge;
  remote: { text: string; html: string; image: Blob | null };
  local: { text: string; html: string; image: Blob | null; isFocused: boolean };
  log: string[];
}

/** ``isRemoteRefusing`` makes every write to the Screen fail, as the service's owner-only image route does for a
 *  visitor. */
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
    async writeRemoteText(text, html) {
      if (isRemoteRefusing) return false;
      remote.text = text;
      remote.html = html;
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

describe("ClipboardBridge", () => {
  it("brings remote text and html to the local clipboard on neko's update", async () => {
    const { bridge, remote, local } = build();
    remote.text = "hello";
    remote.html = "<b>hello</b>";
    await bridge.onRemoteTextUpdated();
    expect(local.text).toBe("hello");
    expect(local.html).toBe("<b>hello</b>");
  });

  it("fetches and writes an image when the service says one is there", async () => {
    const { bridge, remote, local } = build();
    remote.image = new Blob([new Uint8Array([1, 2, 3])], { type: "image/png" });
    await bridge.onRemoteImageUpdated();
    expect(local.image).toBe(remote.image);
  });

  it("sends the local clipboard to the Screen on focus, once per content", async () => {
    const { bridge, remote, local, log } = build();
    local.text = "from laptop";
    await bridge.onFocus();
    await bridge.onFocus();
    expect(remote.text).toBe("from laptop");
    expect(log.filter((entry) => entry.startsWith("remote-text"))).toHaveLength(1);
  });

  it("does not echo what it just received back to the Screen", async () => {
    const { bridge, remote, local, log } = build();
    remote.text = "copied remotely";
    await bridge.onRemoteTextUpdated();
    expect(local.text).toBe("copied remotely");
    await bridge.onFocus();
    expect(log.filter((entry) => entry.startsWith("remote-text"))).toHaveLength(0);
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

  it("reports a paste the Screen refused, and keeps the focus sweep quiet", async () => {
    const { bridge, local, log } = build(true);
    const file = new File([new Uint8Array(16)], "shot.png", { type: "image/png" });
    const imagePaste = {
      items: [{ kind: "file", type: "image/png", getAsFile: () => file }],
      getData: () => "",
    } as unknown as DataTransfer;
    expect(await bridge.onPaste(imagePaste)).toBe(false);
    expect(log).toContain("status:The image could not be pasted on the Screen");
    const textPaste = {
      items: [],
      getData: (type: string) => (type === "text/plain" ? "hello" : ""),
    } as unknown as DataTransfer;
    expect(await bridge.onPaste(textPaste)).toBe(false);
    expect(log).toContain("status:The text could not be pasted on the Screen");
    log.length = 0;
    local.image = file;
    await bridge.onFocus();
    expect(log.filter((entry) => entry.startsWith("status:"))).toEqual([]);
  });

  it("does nothing while this viewer does not control", async () => {
    const { bridge, remote, local } = build();
    bridge.isActive = false;
    remote.text = "secret";
    await bridge.onRemoteTextUpdated();
    expect(local.text).toBe("");
    local.text = "mine";
    await bridge.onFocus();
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
    await bridge.onFocus();
    expect(local.text).toBe("later");
  });
});
