/**
 * The checklist's items as the app serves them (``GET /api/items``), and the reader that keeps them current: it
 * reads when the page loads, when the shell says the window is shown, when the window gets focus, and on a slow
 * timer's tick while the page is on screen. A page that is hidden reads nothing.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { requestJson } from "@imbue/workspace-ui/src/models/http";

export interface ChecklistItem {
  readonly id: string;
  readonly label: string;
  readonly isDone: boolean;
}

/** How often the page reads the items again while it is on screen. */
export const ITEMS_READ_INTERVAL_MS = 10_000;

/** The items of an ``/api/items`` answer, in its order; an entry that is not shaped as one is left out. */
export function parseChecklistItems(body: unknown): ChecklistItem[] {
  if (body === null || typeof body !== "object") return [];
  const entries = (body as { items?: unknown }).items;
  if (!Array.isArray(entries)) return [];
  const items: ChecklistItem[] = [];
  for (const entry of entries as unknown[]) {
    if (entry === null || typeof entry !== "object") continue;
    const { id, label, is_done: isDone } = entry as { id?: unknown; label?: unknown; is_done?: unknown };
    if (typeof id !== "string" || typeof label !== "string") continue;
    items.push({ id, label, isDone: isDone === true });
  }
  return items;
}

export async function fetchChecklistItems(): Promise<ChecklistItem[]> {
  return parseChecklistItems(await requestJson<unknown>("GET", apiUrl("/api/items")));
}

export interface ChecklistReaderDependencies {
  readonly fetchItems: () => Promise<readonly ChecklistItem[]>;
  readonly redraw: () => void;
}

export class ChecklistReader {
  /** The items as last read; null until the first read answers. */
  private items: readonly ChecklistItem[] | null = null;
  // A page takes itself to be shown until its shell says otherwise.
  private isShownByShell = true;
  private isDocumentVisible = true;
  /** Counts the reads, so an answer that arrives after a later read's is dropped. */
  private readCount = 0;

  constructor(private readonly deps: ChecklistReaderDependencies) {}

  getItems(): readonly ChecklistItem[] | null {
    return this.items;
  }

  /** The shell showed the window (``shell:shown``) or put it away (``shell:hidden``). */
  noteShownByShell(isShown: boolean): void {
    this.isShownByShell = isShown;
    if (isShown) this.readWhileOnScreen();
  }

  /** The document became visible, or stopped being: its tab came forward, or went behind another. */
  noteDocumentVisible(isVisible: boolean): void {
    this.isDocumentVisible = isVisible;
    if (isVisible) this.readWhileOnScreen();
  }

  /** The window got focus. */
  noteFocused(): void {
    this.readWhileOnScreen();
  }

  /** The slow timer went off. */
  tick(): void {
    this.readWhileOnScreen();
  }

  private readWhileOnScreen(): void {
    if (this.isShownByShell && this.isDocumentVisible) void this.read();
  }

  /** Read the items now. A read that fails leaves the last items standing. */
  async read(): Promise<void> {
    const count = (this.readCount += 1);
    let items: readonly ChecklistItem[];
    try {
      items = await this.deps.fetchItems();
    } catch (error) {
      console.warn("[onboarding-checklist] could not read the items", error);
      return;
    }
    if (count !== this.readCount) return;
    this.items = items;
    this.deps.redraw();
  }
}
