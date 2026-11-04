/**
 * The phone's modifier bar (docs/system/specs/screen/spec.md section 14): Ctrl, Alt and Shift latch for the next
 * key, Esc and Tab are keys sent at once. A latched modifier is held down on the remote until the next key has
 * gone, then released; tapping a latched modifier again unlatches it.
 */

export const XK_SHIFT_L = 0xffe1;
export const XK_CONTROL_L = 0xffe3;
export const XK_ALT_L = 0xffe9;
export const XK_ESCAPE = 0xff1b;
export const XK_TAB = 0xff09;
export const XK_BACKSPACE = 0xff08;
export const XK_RETURN = 0xff0d;

export type Modifier = "ctrl" | "alt" | "shift";

export const MODIFIER_KEYSYMS: Record<Modifier, number> = {
  ctrl: XK_CONTROL_L,
  alt: XK_ALT_L,
  shift: XK_SHIFT_L,
};

export interface KeySink {
  keyDown(keysym: number): void;
  keyUp(keysym: number): void;
}

export class ModifierLatch {
  private readonly latched = new Set<Modifier>();

  constructor(private readonly sink: KeySink) {}

  isLatched(modifier: Modifier): boolean {
    return this.latched.has(modifier);
  }

  /** Tap a modifier: latch it (held down on the remote), or unlatch it when already latched. */
  toggle(modifier: Modifier): boolean {
    if (this.latched.has(modifier)) {
      this.latched.delete(modifier);
      this.sink.keyUp(MODIFIER_KEYSYMS[modifier]);
      return false;
    }
    this.latched.add(modifier);
    this.sink.keyDown(MODIFIER_KEYSYMS[modifier]);
    return true;
  }

  /** Send a key with every latched modifier held, then release the modifiers. */
  tap(keysym: number): void {
    this.sink.keyDown(keysym);
    this.sink.keyUp(keysym);
    this.releaseAll();
  }

  /** A key went down through another path (the soft keyboard): the latch is spent once it goes up. */
  keyDown(keysym: number): void {
    this.sink.keyDown(keysym);
  }

  keyUp(keysym: number): void {
    this.sink.keyUp(keysym);
    this.releaseAll();
  }

  releaseAll(): void {
    for (const modifier of [...this.latched]) {
      this.latched.delete(modifier);
      this.sink.keyUp(MODIFIER_KEYSYMS[modifier]);
    }
  }
}
