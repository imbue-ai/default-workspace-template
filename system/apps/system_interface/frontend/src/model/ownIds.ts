/**
 * The ids a window mints for what it writes (desktop-interface contracts.md section 1): ``save-<16 hex>``
 * for its placement saves, remembered so the ``placements_updated`` broadcast of the window's own save is
 * told from another window's or the shell's, which the window refetches for; and ``report-<16 hex>`` for
 * the desktop moves it reports, remembered so the ``active_desktop_changed`` echo of a move it has since
 * replaced with another is told from news it has to follow. A window also mints one ``page-<16 hex>`` as its page
 * loads, carried on its ``client_state`` reports.
 */

export const SAVE_ID_PREFIX = "save-";
export const REPORT_ID_PREFIX = "report-";
/** A page's id, minted once as it loads, so the shell can tell the page's own desktop moves from the others'. */
export const PAGE_ID_PREFIX = "page-";
const MINTED_ID_HEX_LENGTH = 16;
// How many of this window's own ids are remembered; a write's broadcast arrives within a round trip, so a
// short memory is plenty.
const REMEMBERED_IDS = 64;

/** Sixteen hex characters from the platform's random source (a weak fallback where there is none). */
export function mintHex(): string {
  const bytes = new Uint8Array(MINTED_ID_HEX_LENGTH / 2);
  if (typeof crypto !== "undefined" && "getRandomValues" in crypto) {
    crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  }
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The memory of one window's own ids of one kind, oldest first. */
export class OwnIdMinter {
  private readonly own: string[] = [];

  constructor(private readonly prefix: string) {}

  /** A fresh id for one write this window makes, remembered so its broadcast is told as its own. */
  mint(): string {
    const id = `${this.prefix}${mintHex()}`;
    this.own.push(id);
    if (this.own.length > REMEMBERED_IDS) this.own.splice(0, this.own.length - REMEMBERED_IDS);
    return id;
  }

  /** Whether this window minted ``id``. */
  isOwn(id: string): boolean {
    return this.own.includes(id);
  }

  /** Whether this window minted ``id`` and has minted another since. */
  isSuperseded(id: string): boolean {
    return this.isOwn(id) && id !== this.own[this.own.length - 1];
  }
}
