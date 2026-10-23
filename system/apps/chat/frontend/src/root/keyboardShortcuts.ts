/** Chat navigation keys, shared by the root and its focused inner chat page. */
export type ChatShortcut = { kind: "new" } | { kind: "index"; index: number } | { kind: "relative"; offset: -1 | 1 };

export function chatShortcut(
  event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey">,
): ChatShortcut | null {
  // Cmd on Mac, Ctrl on Windows/Linux. Accept either without guessing the platform,
  // but leave chords with both modifiers (or Alt) to the browser and operating system.
  if (event.altKey || event.ctrlKey === event.metaKey) return null;
  const key = event.key.toLowerCase();
  if (!event.shiftKey && key === "n") return { kind: "new" };
  if (!event.shiftKey && /^[1-9]$/.test(key)) return { kind: "index", index: Number(key) - 1 };
  if (event.shiftKey) {
    if (key === "[" || key === "{") return { kind: "relative", offset: -1 };
    if (key === "]" || key === "}") return { kind: "relative", offset: 1 };
  }
  return null;
}

export function chatIdForShortcut(
  shortcut: Exclude<ChatShortcut, { kind: "new" }>,
  chatIds: readonly string[],
  selectedId: string | null,
): string | null {
  if (shortcut.kind === "index") return chatIds[shortcut.index] ?? null;
  if (chatIds.length === 0) return null;
  const current = chatIds.indexOf(selectedId ?? "");
  const next =
    current < 0
      ? shortcut.offset === 1
        ? 0
        : chatIds.length - 1
      : (current + shortcut.offset + chatIds.length) % chatIds.length;
  return chatIds[next] ?? null;
}
