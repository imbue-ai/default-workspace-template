/**
 * The save ids a window mints for its placement saves (desktop-interface contracts.md section 1):
 * ``save-<16 hex>``, remembered so the ``placements_updated`` broadcast of the window's own save
 * is told from another window's or the shell's, which the window refetches for.
 */

const SAVE_ID_PREFIX = "save-";
const MINTED_ID_HEX_LENGTH = 16;
// How many of this window's own save ids are remembered; a save's broadcast arrives within a
// round trip, so a short memory is plenty.
const REMEMBERED_SAVE_IDS = 64;

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

/** The memory of one window's own save ids. */
export class SaveIdMinter {
  private readonly own: string[] = [];

  /** A fresh save id for one save this window makes, remembered so its broadcast is skipped. */
  mint(): string {
    const saveId = `${SAVE_ID_PREFIX}${mintHex()}`;
    this.own.push(saveId);
    if (this.own.length > REMEMBERED_SAVE_IDS) this.own.splice(0, this.own.length - REMEMBERED_SAVE_IDS);
    return saveId;
  }

  /** Whether this window minted ``saveId``. */
  isOwn(saveId: string): boolean {
    return this.own.includes(saveId);
  }
}
