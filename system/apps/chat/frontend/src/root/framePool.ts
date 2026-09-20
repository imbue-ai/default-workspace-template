/**
 * The chat root's inner frames: one iframe of ``/<chat-id>`` per chat the root has shown this
 * session, the selected one visible and the rest hidden, so switching back is instant.
 *
 * Bounded: past ``MAX_HELD_FRAMES`` the frame shown longest ago is destroyed. A hidden frame is
 * told ``hidden`` and a shown one ``shown`` through the page's embed API (the two documents
 * share an origin), which is what the page keys its presence reports on, so a chat held here
 * but not on screen counts as open-but-hidden, like a minimized window's page.
 */

import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { getBasePath } from "@imbue/workspace-ui/src/base-path";
import type { ChatPageEmbedApi } from "../embedApi";

export const MAX_HELD_FRAMES = 4;

// What the shell grants an app page's frame (the PAGE_FRAME_SANDBOX of its live pages layer); a
// nested frame can hold no more than its parent, so this asks for the same.
const FRAME_SANDBOX =
  "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals";
const FRAME_ALLOW = "clipboard-read; clipboard-write";

interface HeldFrame {
  frame: HTMLIFrameElement;
  lastShownAt: number;
  isLoaded: boolean;
}

export class InnerFramePool {
  private readonly held = new Map<string, HeldFrame>();
  private shownChatId: string | null = null;
  private handshake: ShellHandshake | null = null;
  private isRootShown = false;

  constructor(private readonly container: HTMLElement) {}

  /** Whether ``source`` is the window of one of this pool's frames. */
  isInnerWindow(source: MessageEventSource | null): boolean {
    if (source === null) return false;
    for (const { frame } of this.held.values()) {
      if (frame.contentWindow === source) return true;
    }
    return false;
  }

  /** Show ``chatId`` (creating its frame on first sight), hiding whatever was shown; null shows nothing. */
  show(chatId: string | null): void {
    if (chatId !== null && !this.held.has(chatId)) this.create(chatId);
    const previous = this.shownChatId;
    this.shownChatId = chatId;
    if (previous !== null && previous !== chatId) this.tell(previous, "hidden");
    for (const [heldId, held] of this.held) {
      held.frame.hidden = heldId !== chatId;
    }
    if (chatId !== null) {
      const held = this.held.get(chatId);
      if (held !== undefined) held.lastShownAt = Date.now();
      if (this.isRootShown) this.tell(chatId, "shown");
    }
    this.evict();
  }

  /** The shell's word on whether the root is on screen, passed to the shown page. */
  setRootShown(isShown: boolean): void {
    this.isRootShown = isShown;
    if (this.shownChatId !== null) this.tell(this.shownChatId, isShown ? "shown" : "hidden");
  }

  /** The handshake the shell gave the root, handed to every page (now and as each loads). */
  setHandshake(handshake: ShellHandshake): void {
    this.handshake = handshake;
    for (const chatId of this.held.keys()) this.introduce(chatId);
  }

  /** Drop a chat's frame (the chat was deleted). */
  destroy(chatId: string): void {
    const held = this.held.get(chatId);
    if (held === undefined) return;
    held.frame.remove();
    this.held.delete(chatId);
    if (this.shownChatId === chatId) this.shownChatId = null;
  }

  heldChatIds(): string[] {
    return [...this.held.keys()];
  }

  private create(chatId: string): void {
    const frame = document.createElement("iframe");
    frame.src = `${getBasePath()}/${encodeURIComponent(chatId)}`;
    frame.title = "Chat";
    frame.setAttribute("sandbox", FRAME_SANDBOX);
    frame.setAttribute("allow", FRAME_ALLOW);
    frame.className = "chat-root-frame absolute inset-0 h-full w-full border-0";
    frame.dataset.chatId = chatId;
    frame.hidden = true;
    const held: HeldFrame = { frame, lastShownAt: Date.now(), isLoaded: false };
    frame.addEventListener("load", () => {
      held.isLoaded = true;
      this.introduce(chatId);
      this.tell(chatId, this.isRootShown && this.shownChatId === chatId ? "shown" : "hidden");
    });
    this.held.set(chatId, held);
    this.container.appendChild(frame);
  }

  private api(chatId: string): ChatPageEmbedApi | null {
    const held = this.held.get(chatId);
    if (held === undefined || !held.isLoaded) return null;
    return held.frame.contentWindow?.chatPageEmbed ?? null;
  }

  private introduce(chatId: string): void {
    if (this.handshake === null) return;
    this.api(chatId)?.handshake(this.handshake);
  }

  private tell(chatId: string, what: "shown" | "hidden"): void {
    const api = this.api(chatId);
    if (api === null) return;
    if (what === "shown") api.shown();
    else api.hidden();
  }

  private evict(): void {
    while (this.held.size > MAX_HELD_FRAMES) {
      let oldest: [string, HeldFrame] | null = null;
      for (const entry of this.held) {
        if (entry[0] === this.shownChatId) continue;
        if (oldest === null || entry[1].lastShownAt < oldest[1].lastShownAt) oldest = entry;
      }
      if (oldest === null) return;
      this.destroy(oldest[0]);
    }
  }
}
