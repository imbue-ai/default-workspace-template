/**
 * The clipboard across the boundary (docs/system/specs/screen/spec.md section 13), while this viewer holds
 * control. Remote to local: neko's ``clipboard/updated`` reaches the host session, and the viewer then reads text
 * and HTML from neko's clipboard route and writes them locally; the service's ``clipboard`` event says an image is
 * there, which the viewer fetches from neko's image route and writes. Local to remote: on paste, while this viewer
 * holds control, the paste event's plain text goes to neko's clipboard route and an image to the service's
 * ``/api/clipboard/image`` as this session, which the service accepts from the session in control alone (a visitor
 * in control included). Nothing reads the local clipboard on its own: a programmatic read makes Safari and Firefox
 * pop a "Paste" button and Chrome a permission prompt on every click into the Screen, while the paste event carries
 * its data with no permission at all. A paste that does not cross says so in the strip. A local write needs a
 * focused document, so one that fails is kept for the next focus.
 *
 * neko owns the X clipboard through ``xclip``, which shapes both directions. It writes one target (``text/html``
 * when any HTML is sent, else ``UTF8_STRING``), so only the plain text is sent: a URL bar or a terminal on the
 * Screen has nothing to paste from an HTML-only clipboard. And an ``xclip`` owner answers a request for any target
 * with its one payload, so neko's read-back of its own write reports HTML equal to the text, and neko announces
 * that write as a ``clipboard/updated`` like any other. The echo is told apart by plain text alone, and HTML equal
 * to the text is treated as no HTML.
 */

export const CLIPBOARD_MAX_BYTES = 10 * 1024 * 1024;

export interface ClipboardTransport {
  /** neko's clipboard text and HTML (host only). */
  readRemoteText(): Promise<{ text: string; html: string } | null>;
  /** neko's clipboard image as PNG (host only), or null when it has none. */
  readRemoteImage(): Promise<Blob | null>;
  writeRemoteText(text: string): Promise<boolean>;
  writeRemoteImage(image: Blob): Promise<boolean>;
}

export interface LocalClipboard {
  write(items: { text?: string; html?: string; image?: Blob }): Promise<void>;
  read(): Promise<{ text: string; html: string; image: Blob | null } | null>;
}

/** neko's route with the session token in the query string, as the WebSocket carries it. A bearer header
 *  would never reach neko over a share: the share gateway reads any ``Authorization: Bearer`` as a visiting
 *  agent's admission token and refuses the request, and strips the header from what it forwards. */
export function nekoRouteWithToken(path: string, token: string | null): string {
  return token === null ? path : `${path}?token=${encodeURIComponent(token)}`;
}

/** The transport over the service's origin: neko's routes under ``/neko/`` with the session's token, and the
 *  service's image route as the session's id. */
export function serviceClipboardTransport(
  token: () => string | null,
  sessionId: () => string | null,
): ClipboardTransport {
  return {
    async readRemoteText() {
      const response = await fetch(nekoRouteWithToken("/neko/api/room/clipboard", token()));
      if (!response.ok) return null;
      const body = (await response.json()) as { text?: string; html?: string };
      return { text: body.text ?? "", html: body.html ?? "" };
    },
    async readRemoteImage() {
      const response = await fetch(nekoRouteWithToken("/neko/api/room/clipboard/image.png", token()));
      if (!response.ok) return null;
      const blob = await response.blob();
      return blob.size > 0 ? blob : null;
    },
    async writeRemoteText(text) {
      const response = await fetch(nekoRouteWithToken("/neko/api/room/clipboard", token()), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, html: "" }),
      });
      return response.ok;
    },
    async writeRemoteImage(image) {
      const session = sessionId();
      if (session === null) return false;
      const form = new FormData();
      form.set("session_id", session);
      form.set("image", image, "clipboard");
      const response = await fetch("/api/clipboard/image", { method: "POST", body: form });
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

async function hashOf(image: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-1", await image.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The HTML neko reports for the remote clipboard, or nothing when it is the plain text again: an ``xclip`` owner
 *  (everything neko itself wrote) answers every target with its one payload, so its HTML is never rich. */
export function remoteHtmlOf(remote: { text: string; html: string }): string {
  return remote.html === remote.text ? "" : remote.html;
}

export class ClipboardBridge {
  private pendingLocalWrite: (() => Promise<void>) | null = null;
  /** The text the remote clipboard holds as far as this viewer knows (sent there, or brought from there). */
  private knownRemoteText: string | null = null;
  /** The text whose echo from neko is still expected; cleared once the remote clipboard moves on. */
  private lastSentText: string | null = null;
  private knownRemoteImageHash: string | null = null;
  private lastSentImageHash: string | null = null;
  /** Whether this viewer holds control (set by the viewer); nothing crosses otherwise. */
  isActive = false;

  constructor(
    private readonly transport: ClipboardTransport,
    private readonly local: LocalClipboard,
    private readonly onStatus: (message: string) => void = () => undefined,
  ) {}

  /** neko said the remote clipboard changed (the host hears this): bring text and HTML over, unless it is the
   *  echo of what this viewer just sent. */
  async onRemoteTextUpdated(): Promise<void> {
    if (!this.isActive) return;
    const remote = await this.transport.readRemoteText().catch(() => null);
    if (remote === null || remote.text === "") return;
    if (remote.text === this.lastSentText) return;
    this.lastSentText = null;
    this.knownRemoteText = remote.text;
    const html = remoteHtmlOf(remote);
    await this.writeLocally(() => this.local.write({ text: remote.text, html }), "Copied from the Screen");
  }

  /** The service said the remote clipboard holds an image: bring it over. */
  async onRemoteImageUpdated(): Promise<void> {
    if (!this.isActive) return;
    const image = await this.transport.readRemoteImage().catch(() => null);
    if (image === null) return;
    const hash = await hashOf(image);
    if (hash === this.lastSentImageHash) return;
    this.lastSentImageHash = null;
    this.knownRemoteImageHash = hash;
    await this.writeLocally(() => this.local.write({ image }), "Image copied from the Screen");
  }

  /** The document got focus: a local write that failed for want of focus runs now. The local clipboard is not
   *  read: that is the paste event's job (see the module comment). */
  onFocus(): void {
    this.flushPendingLocalWrite();
  }

  /** A paste event: its data is readable at once, with no permission, so it is the surest path to the Screen.
   *  Answers whether the remote clipboard now holds the pasted content. */
  async onPaste(data: DataTransfer): Promise<boolean> {
    if (!this.isActive) return false;
    for (const item of Array.from(data.items)) {
      if (item.kind === "file" && item.type.startsWith("image/")) {
        const image = item.getAsFile();
        if (image !== null) return this.sendImageToRemote(image);
      }
    }
    return this.sendTextToRemote(data.getData("text/plain"));
  }

  private async sendImageToRemote(image: Blob): Promise<boolean> {
    if (image.size > CLIPBOARD_MAX_BYTES) {
      this.onStatus("The image is too large to paste");
      return false;
    }
    const hash = await hashOf(image);
    if (hash === this.knownRemoteImageHash) return true;
    const isSent = await this.transport.writeRemoteImage(image).catch(() => false);
    if (isSent) {
      this.knownRemoteImageHash = hash;
      this.lastSentImageHash = hash;
    } else {
      this.onStatus("The image could not be pasted on the Screen");
    }
    return isSent;
  }

  /** Only the plain text crosses (see the module comment); a text the Screen already holds is not sent again. */
  private async sendTextToRemote(text: string): Promise<boolean> {
    if (text === "") {
      this.onStatus("There is no text to paste on the Screen");
      return false;
    }
    if (text === this.knownRemoteText) return true;
    const isSent = await this.transport.writeRemoteText(text).catch(() => false);
    if (isSent) {
      this.knownRemoteText = text;
      this.lastSentText = text;
    } else {
      this.onStatus("The text could not be pasted on the Screen");
    }
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
