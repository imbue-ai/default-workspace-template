/**
 * The workspace's keyboard chords, and the platform question they turn on.
 *
 * A chord reaches the workspace two ways, because every tab is a frame: a press in the shell's own
 * chrome lands in the shell document, and a press anywhere else lands in an app page, which carries
 * it up over the app contract. The shell reads this module for the first path. The contract module
 * cannot -- it is served standalone to every app origin and so may import nothing -- and carries a
 * private copy for the second; `app_contract.test.ts` pins the two together.
 */

/** The keydown fields a chord is decided from. */
export interface ChordKeys {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

/**
 * Whether this browser reports an Apple platform, whose accelerator key is Cmd rather than Ctrl.
 *
 * Prefers `userAgentData` over the deprecated `navigator.platform`, with the UA string as the
 * fallback -- the shape `models/ClientIdentity` uses for the device kind.
 */
export function isApplePlatform(): boolean {
  const uaData = (navigator as { userAgentData?: { platform?: string } }).userAgentData;
  return /mac|iphone|ipad|ipod/i.test(uaData?.platform ?? navigator.userAgent);
}

/**
 * Whether a keydown is the workspace's new-tab chord: Cmd+T on Apple platforms, Ctrl+T elsewhere.
 *
 * Only the desktop client delivers it to a page at all; every browser keeps Cmd/Ctrl+T for a
 * browser tab of its own.
 */
export function isNewTabChord(keys: ChordKeys, isApple: boolean): boolean {
  if (keys.key !== "t" && keys.key !== "T") return false;
  if (keys.altKey || keys.shiftKey) return false;
  return isApple ? keys.metaKey && !keys.ctrlKey : keys.ctrlKey && !keys.metaKey;
}
