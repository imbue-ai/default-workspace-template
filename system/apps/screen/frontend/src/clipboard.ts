/**
 * The clipboard across the boundary (docs/system/specs/screen/spec.md section 13), while this viewer holds
 * control. Remote to local: neko's ``clipboard/updated`` reaches the host session, and the viewer then reads text
 * and HTML from neko's clipboard route and writes them locally; the service's ``clipboard`` event says an image is
 * there, which the viewer fetches from neko's image route and writes. Local to remote: on focus and on paste the
 * viewer reads the local clipboard; text and HTML go to neko's clipboard route (its ``clipboard/set`` message
 * carries text alone), an image to the service's ``/api/clipboard/image`` (the owner's route: a visitor's image
 * is refused). A paste that does not cross says so in the strip; the focus sweep stays quiet. A local write needs
 * a focused document, so one that fails is kept for the next focus.
 */

export const CLIPBOARD_MAX_BYTES = 10 * 1024 * 1024;

export interface ClipboardTransport {
  /** neko's clipboard text and HTML (host only). */
  readRemoteText(): Promise<{ text: string; html: string } | null>;
  /** neko's clipboard image as PNG (host only), or null when it has none. */
  readRemoteImage(): Promise<Blob | null>;
  writeRemoteText(text: string, html: string): Promise<boolean>;
  writeRemoteImage(image: Blob): Promise<boolean>;
}

export interface LocalClipboard {
  write(items: { text?: string; html?: string; image?: Blob }): Promise<void>;
  read(): Promise<{ text: string; html: string; image: Blob | null } | null>;
}

/** The transport over the service's origin: neko's routes under ``/neko/`` with the session's token. */
export function serviceClipboardTransport(token: () => string | null): ClipboardTransport {
  const headers = (): HeadersInit => {
    const bearer = token();
    return bearer ? { Authorization: `Bearer ${bearer}` } : {};
  };
  return {
    async readRemoteText() {
      const response = await fetch("/neko/api/room/clipboard", { headers: headers() });
      if (!response.ok) return null;
      const body = (await response.json()) as { text?: string; html?: string };
      return { text: body.text ?? "", html: body.html ?? "" };
    },
    async readRemoteImage() {
      const response = await fetch("/neko/api/room/clipboard/image.png", { headers: headers() });
      if (!response.ok) return null;
      const blob = await response.blob();
      return blob.size > 0 ? blob : null;
    },
    async writeRemoteText(text, html) {
      const response = await fetch("/neko/api/room/clipboard", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ text, html }),
      });
      return response.ok;
    },
    async writeRemoteImage(image) {
      const response = await fetch("/api/clipboard/image", {
        method: "POST",
        headers: { "Content-Type": image.type || "image/png" },
        body: image,
      });
      return response.ok;
    },
  };
}

/** The browser's Clipboard API; Chromium writes only PNG images, so anything else is transcoded first. */
export const navigatorClipboard: LocalClipboard = {
  async write(items) {
    const parts: Record<string, Blob> = {};
    if (items.text !== undefined && items.text !== "")
      parts["text/plain"] = new Blob([items.text], { type: "text/plain" });
    if (items.html !== undefined && items.html !== "")
      parts["text/html"] = new Blob([items.html], { type: "text/html" });
    if (items.image) parts["image/png"] = items.image.type === "image/png" ? items.image : await toPng(items.image);
    if (Object.keys(parts).length === 0) return;
    if (typeof ClipboardItem === "undefined") {
      if (items.text !== undefined) await navigator.clipboard.writeText(items.text);
      return;
    }
    await navigator.clipboard.write([new ClipboardItem(parts)]);
  },
  async read() {
    if (typeof navigator.clipboard.read !== "function") return null;
    const items = await navigator.clipboard.read();
    let text = "";
    let html = "";
    let image: Blob | null = null;
    for (const item of items) {
      for (const type of item.types) {
        if (type === "text/plain") text = await (await item.getType(type)).text();
        else if (type === "text/html") html = await (await item.getType(type)).text();
        else if (type.startsWith("image/") && image === null) image = await item.getType(type);
      }
    }
    return { text, html, image };
  },
};

async function toPng(image: Blob): Promise<Blob> {
  const bitmap = await createImageBitmap(image);
  const canvas = document.createElement("canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
  return new Promise((resolve, reject) =>
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("the image could not be encoded"))), "image/png"),
  );
}

async function hashOf(parts: (string | Blob)[]): Promise<string> {
  const buffers: ArrayBuffer[] = [];
  for (const part of parts)
    buffers.push(typeof part === "string" ? new TextEncoder().encode(part).buffer : await part.arrayBuffer());
  const joined = new Blob(buffers);
  const digest = await crypto.subtle.digest("SHA-1", await joined.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export class ClipboardBridge {
  private pendingLocalWrite: (() => Promise<void>) | null = null;
  private lastSentHash: string | null = null;
  private lastReceivedHash: string | null = null;
  /** Whether this viewer holds control (set by the viewer); nothing crosses otherwise. */
  isActive = false;

  constructor(
    private readonly transport: ClipboardTransport,
    private readonly local: LocalClipboard,
    private readonly onStatus: (message: string) => void = () => undefined,
  ) {}

  /** neko said the remote clipboard changed (the host hears this): bring text and HTML over. */
  async onRemoteTextUpdated(): Promise<void> {
    if (!this.isActive) return;
    const remote = await this.transport.readRemoteText().catch(() => null);
    if (remote === null || (remote.text === "" && remote.html === "")) return;
    const hash = await hashOf([remote.text, remote.html]);
    if (hash === this.lastSentHash) return;
    this.lastReceivedHash = hash;
    await this.writeLocally(
      () => this.local.write({ text: remote.text, html: remote.html }),
      "Copied from the Screen",
    );
  }

  /** The service said the remote clipboard holds an image: bring it over. */
  async onRemoteImageUpdated(): Promise<void> {
    if (!this.isActive) return;
    const image = await this.transport.readRemoteImage().catch(() => null);
    if (image === null) return;
    const hash = await hashOf([image]);
    if (hash === this.lastSentHash) return;
    this.lastReceivedHash = hash;
    await this.writeLocally(() => this.local.write({ image }), "Image copied from the Screen");
  }

  /** The document got focus: whatever the local clipboard holds goes to the Screen, once per content. */
  async onFocus(): Promise<void> {
    this.flushPendingLocalWrite();
    if (!this.isActive) return;
    const contents = await this.local.read().catch(() => null);
    if (contents === null) return;
    await this.sendToRemote(contents.text, contents.html, contents.image, false);
  }

  /** A paste event: its data is readable at once, with no permission, so it is the surest path to the Screen. */
  async onPaste(data: DataTransfer): Promise<boolean> {
    if (!this.isActive) return false;
    let image: Blob | null = null;
    for (const item of Array.from(data.items)) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        image = item.getAsFile();
        break;
      }
    }
    return this.sendToRemote(data.getData("text/plain"), data.getData("text/html"), image, true);
  }

  /** Sends the content once per hash; ``isPaste`` is the user's own paste, whose failure the strip reports. */
  private async sendToRemote(text: string, html: string, image: Blob | null, isPaste: boolean): Promise<boolean> {
    if (image !== null) {
      if (image.size > CLIPBOARD_MAX_BYTES) {
        this.onStatus("The image is too large to paste");
        return false;
      }
      const hash = await hashOf([image]);
      if (hash === this.lastSentHash || hash === this.lastReceivedHash) return true;
      const isSent = await this.transport.writeRemoteImage(image).catch(() => false);
      if (isSent) this.lastSentHash = hash;
      else if (isPaste) this.onStatus("The image could not be pasted on the Screen");
      return isSent;
    }
    if (text === "" && html === "") return false;
    const hash = await hashOf([text, html]);
    if (hash === this.lastSentHash || hash === this.lastReceivedHash) return true;
    const isSent = await this.transport.writeRemoteText(text, html).catch(() => false);
    if (isSent) this.lastSentHash = hash;
    else if (isPaste) this.onStatus("The text could not be pasted on the Screen");
    return isSent;
  }

  private async writeLocally(write: () => Promise<void>, message: string): Promise<void> {
    try {
      await write();
      this.onStatus(message);
    } catch {
      // The document is not focused: the write runs on the next focus.
      this.pendingLocalWrite = write;
      this.onStatus("Copied; click the Screen to sync your clipboard");
    }
  }

  private flushPendingLocalWrite(): void {
    const write = this.pendingLocalWrite;
    this.pendingLocalWrite = null;
    if (write) void write().catch(() => undefined);
  }
}
