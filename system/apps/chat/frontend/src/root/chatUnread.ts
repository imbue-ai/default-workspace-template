/**
 * Which chats have finished a turn the user has not looked at yet: the "done, come back to me"
 * mark the list shows, so a chat that answered while the user was elsewhere stands out from
 * one merely sitting idle.
 *
 * Read off the chat list: a chat whose status goes from working to anything else while the
 * root is not showing it on screen is marked, and the mark goes when the root shows it, or
 * when it starts working again (a new turn makes the old reply old news). Kept per browser,
 * in storage, so it survives a reload and every root of this browser agrees.
 */

const STORAGE_KEY = "chat-root-unread";

let unreadChatIds = new Set<string>();
let lastStatusByChatId = new Map<string, string>();

function load(): Set<string> {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw === null ? [] : JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === "string") : []);
  } catch {
    return new Set();
  }
}

function save(): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...unreadChatIds]));
  } catch {
    // A browser that refuses storage still gets the state for this page's lifetime.
  }
}

export function initChatUnread(): void {
  unreadChatIds = load();
  window.addEventListener("storage", (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) unreadChatIds = load();
  });
}

/** Fold one chat list push in: ``statusByChatId`` is every listed chat's status, and
 *  ``shownChatId`` the chat the root is showing on screen right now (null for none). */
export function noteStatuses(statusByChatId: ReadonlyMap<string, string>, shownChatId: string | null): void {
  let isChanged = false;
  for (const [chatId, status] of statusByChatId) {
    const previous = lastStatusByChatId.get(chatId);
    if (status === "working") {
      if (unreadChatIds.delete(chatId)) isChanged = true;
    } else if (previous === "working" && chatId !== shownChatId) {
      unreadChatIds.add(chatId);
      isChanged = true;
    }
  }
  for (const chatId of unreadChatIds) {
    if (!statusByChatId.has(chatId) || chatId === shownChatId) {
      unreadChatIds.delete(chatId);
      isChanged = true;
    }
  }
  lastStatusByChatId = new Map(statusByChatId);
  if (isChanged) save();
}

export function isUnread(chatId: string): boolean {
  return unreadChatIds.has(chatId);
}

/** The user is looking at it: whatever it finished has been seen. */
export function markRead(chatId: string): void {
  if (unreadChatIds.delete(chatId)) save();
}
