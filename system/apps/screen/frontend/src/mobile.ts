/**
 * The phone layout's controls (docs/system/specs/screen/spec.md section 14): touch on the stage classified into
 * taps, long presses, drags, pinches and two-finger scrolls; a bar with Ctrl, Alt, Shift (latching for the next
 * key), Esc, Tab, Scroll Up and Scroll Down; and a keyboard button focusing a hidden input whose ``input`` and
 * ``keydown`` events become neko key events. A tap reaches the Screen as a touch at the mapped point (through neko's
 * input driver), a long press as a right click there (X touch has no second button and a context menu is what the
 * press is for), and a one-finger drag as a touch held and moved on the Screen, so a page scrolls under the finger
 * as on any phone; once the video is pinch-zoomed the drag pans the view instead (the Screen keeps its size).
 */

import { PHONE_MEDIA_QUERY, TOUCH_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import { GestureClassifier, LONG_PRESS_MS } from "./gestures";
import type { Gesture, PointerSample } from "./gestures";
import { ModifierLatch, XK_BACKSPACE, XK_ESCAPE, XK_RETURN, XK_TAB } from "./modifier_latch";
import type { KeySink, Modifier } from "./modifier_latch";
import type { Scheduler } from "./scheduler";
import { IDENTITY_TRANSFORM, panBy, transformToCss, zoomAbout } from "./view_transform";
import type { Point, ViewTransform } from "./view_transform";

/** What the controls do to the Screen; the viewer wires these to the input relay. */
export interface MobileActions {
  /** A tap at a client point, with the finger's pressure (0 to 1) as the pointer event reported it. */
  tapAt(clientX: number, clientY: number, pressure: number): void;
  /** A click at a client point: left (1) or right (3) button. */
  clickAt(clientX: number, clientY: number, button: number): void;
  /** Wheel steps in the DOM's sign (positive y scrolls down). */
  scroll(steps: Point): void;
  /** A touch held on the Screen from a client point; false when none began (no input driver, or off the video). */
  touchBegin(clientX: number, clientY: number, pressure: number): boolean;
  touchMove(clientX: number, clientY: number, pressure: number): void;
  touchEnd(clientX: number, clientY: number, pressure: number): void;
}

const X_BUTTON_RIGHT = 3;
/** How many wheel steps one tap of Scroll Up or Scroll Down sends: about a third of a page in a browser. */
export const SCROLL_BUTTON_STEPS = 3;

/** Whether this viewer is a phone: a phone-sized viewport (the shared query the shell switches on) with a finger as
 *  the primary pointer. A desktop window that is merely small is not one; it reports its size and the Screen fits
 *  it, while a phone keeps the Screen's size and gets these controls (spec section 14). */
export function isPhoneViewer(matches: (query: string) => boolean): boolean {
  return matches(PHONE_MEDIA_QUERY) && matches(TOUCH_MEDIA_QUERY);
}

/** The non-character keys a soft keyboard sends through ``keydown`` with a name, as X keysyms. */
const NAMED_KEYSYMS: Record<string, number> = {
  Enter: XK_RETURN,
  Backspace: XK_BACKSPACE,
  Tab: XK_TAB,
  Escape: XK_ESCAPE,
  Delete: 0xffff,
  ArrowLeft: 0xff51,
  ArrowUp: 0xff52,
  ArrowRight: 0xff53,
  ArrowDown: 0xff54,
  Home: 0xff50,
  End: 0xff57,
  PageUp: 0xff55,
  PageDown: 0xff56,
};

export class MobileControls {
  readonly bar = document.createElement("div");
  private readonly keyboardInput = document.createElement("input");
  private readonly classifier = new GestureClassifier();
  private readonly latch: ModifierLatch;
  private readonly modifierButtons = new Map<Modifier, HTMLButtonElement>();
  private transform: ViewTransform = IDENTITY_TRANSFORM;
  private longPressTimer: number | null = null;
  /** The pressure of the finger that went down last, for the tap it may turn out to be. */
  private lastDownPressure = 0;
  /** The pressure the moving finger last reported, for the touch drag it feeds. */
  private lastMovePressure = 0;
  /** Whether a one-finger drag is a touch held on the Screen right now. */
  private isTouchDragging = false;
  /** Whether this drag has tried to begin its touch where the finger came down. */
  private isDragStartTried = false;
  private lastInputValue = "";
  isEnabled = false;

  constructor(
    private readonly stage: HTMLElement,
    private readonly video: HTMLElement,
    private readonly actions: MobileActions,
    keys: KeySink,
    private readonly scheduler: Scheduler,
  ) {
    this.latch = new ModifierLatch(keys);
    this.bar.className = "screen-mobile-bar";
    this.bar.dataset.screenMobileBar = "true";
    for (const [label, modifier] of [
      ["Ctrl", "ctrl"],
      ["Alt", "alt"],
      ["Shift", "shift"],
    ] as const) {
      const button = this.makeButton(label);
      button.dataset.screenModifier = modifier;
      button.addEventListener("click", () => {
        const isLatched = this.latch.toggle(modifier);
        button.setAttribute("aria-pressed", String(isLatched));
      });
      this.modifierButtons.set(modifier, button);
    }
    const escape = this.makeButton("Esc");
    escape.addEventListener("click", () => this.tapKey(XK_ESCAPE));
    const tab = this.makeButton("Tab");
    tab.addEventListener("click", () => this.tapKey(XK_TAB));
    const scrollUp = this.makeButton("Scroll Up");
    scrollUp.dataset.screenScroll = "up";
    scrollUp.addEventListener("click", () => this.scrollBy(-SCROLL_BUTTON_STEPS));
    const scrollDown = this.makeButton("Scroll Down");
    scrollDown.dataset.screenScroll = "down";
    scrollDown.addEventListener("click", () => this.scrollBy(SCROLL_BUTTON_STEPS));
    const keyboard = this.makeButton("Keyboard");
    keyboard.dataset.screenKeyboard = "true";
    keyboard.addEventListener("click", () => this.keyboardInput.focus());
    this.keyboardInput.className = "screen-mobile-keyboard-input";
    this.keyboardInput.setAttribute("aria-label", "Type on the Screen");
    this.keyboardInput.autocapitalize = "off";
    this.keyboardInput.autocomplete = "off";
    this.keyboardInput.spellcheck = false;
    this.bar.append(this.keyboardInput);
    this.wireKeyboardInput();
    this.wireStage();
  }

  setEnabled(isEnabled: boolean): void {
    this.isEnabled = isEnabled;
    if (!isEnabled) {
      // The input relay ends a held touch itself when it is disabled; the drag is over here too.
      this.isTouchDragging = false;
      this.latch.releaseAll();
      for (const button of this.modifierButtons.values()) button.setAttribute("aria-pressed", "false");
    }
  }

  get viewTransform(): ViewTransform {
    return this.transform;
  }

  private makeButton(label: string): HTMLButtonElement {
    const button = document.createElement("button");
    button.className = "screen-button screen-mobile-bar__key";
    button.textContent = label;
    button.setAttribute("aria-pressed", "false");
    this.bar.append(button);
    return button;
  }

  private tapKey(keysym: number): void {
    if (!this.isEnabled) return;
    this.latch.tap(keysym);
    this.syncModifierButtons();
  }

  private scrollBy(steps: number): void {
    if (!this.isEnabled) return;
    this.actions.scroll({ x: 0, y: steps });
  }

  private syncModifierButtons(): void {
    for (const [modifier, button] of this.modifierButtons) {
      button.setAttribute("aria-pressed", String(this.latch.isLatched(modifier)));
    }
  }

  private wireKeyboardInput(): void {
    // Characters typed come through ``input``: each new one is typed on the remote as its Unicode keysym; a
    // deletion is Backspace. Non-character keys (Enter, Backspace with an empty field, arrows) come through
    // ``keydown`` with a code the Guacamole table maps.
    this.keyboardInput.addEventListener("input", (event) => {
      if (!this.isEnabled) return;
      const value = this.keyboardInput.value;
      const inputEvent = event as InputEvent;
      if (inputEvent.inputType.startsWith("delete") || value.length < this.lastInputValue.length) {
        this.latch.tap(XK_BACKSPACE);
      } else {
        for (const character of value.slice(this.lastInputValue.length)) {
          this.latch.tap(unicodeKeysym(character));
        }
      }
      this.lastInputValue = value;
      if (value.length > 64) {
        this.keyboardInput.value = "";
        this.lastInputValue = "";
      }
      this.syncModifierButtons();
    });
    this.keyboardInput.addEventListener("keydown", (event) => {
      if (!this.isEnabled) return;
      // Backspace with text in the field deletes a character, which ``input`` reports; with none, nothing would
      // fire, so it goes out from here.
      if (event.key === "Backspace" && this.keyboardInput.value !== "") return;
      const keysym = NAMED_KEYSYMS[event.key];
      if (keysym === undefined) return;
      event.preventDefault();
      this.latch.tap(keysym);
      this.syncModifierButtons();
    });
  }

  private wireStage(): void {
    this.stage.style.touchAction = "none";
    this.stage.addEventListener("pointerdown", (event) => {
      if (event.pointerType !== "touch") return;
      try {
        this.stage.setPointerCapture(event.pointerId);
      } catch {
        // A pointer the browser does not track (a synthesized event) cannot be captured; the gesture still counts.
      }
      this.lastDownPressure = event.pressure;
      this.apply(this.classifier.down(sampleOf(event)));
      this.cancelLongPress();
      if (this.classifier.pointerCount === 1) {
        this.isDragStartTried = false;
        this.longPressTimer = this.scheduler.setTimeout(() => {
          this.longPressTimer = null;
          this.apply(this.classifier.longPress());
        }, LONG_PRESS_MS);
      }
      event.preventDefault();
    });
    this.stage.addEventListener("pointermove", (event) => {
      if (event.pointerType !== "touch") return;
      this.lastMovePressure = event.pressure;
      const gesture = this.classifier.move(sampleOf(event));
      if (gesture.kind !== "none") this.cancelLongPress();
      this.apply(gesture);
    });
    const end = (event: PointerEvent) => {
      if (event.pointerType !== "touch") return;
      this.cancelLongPress();
      this.apply(
        event.type === "pointercancel" ? this.classifier.cancel(event.pointerId) : this.classifier.up(sampleOf(event)),
      );
    };
    this.stage.addEventListener("pointerup", end);
    this.stage.addEventListener("pointercancel", end);
    this.stage.addEventListener("contextmenu", (event) => event.preventDefault());
  }

  private cancelLongPress(): void {
    if (this.longPressTimer !== null) {
      this.scheduler.clearTimeout(this.longPressTimer);
      this.longPressTimer = null;
    }
  }

  private apply(gesture: Gesture): void {
    const rect = this.stage.getBoundingClientRect();
    const stageSize = { width: rect.width, height: rect.height };
    switch (gesture.kind) {
      case "none":
        return;
      case "tap":
        if (this.isEnabled) this.actions.tapAt(gesture.at.x, gesture.at.y, this.lastDownPressure);
        return;
      case "long-press":
        if (this.isEnabled) this.actions.clickAt(gesture.at.x, gesture.at.y, X_BUTTON_RIGHT);
        return;
      case "pan":
        // Zoomed in, the finger moves the view; at the Screen's own scale it is a touch on the Screen, which is
        // what makes a page scroll. Without neko's input driver (no touch began) the view is panned as before.
        if (this.transform.zoom > 1) {
          this.setTransform(panBy(this.transform, gesture.delta, stageSize));
          return;
        }
        if (!this.isEnabled) return;
        if (!this.isTouchDragging) {
          // The touch begins where the finger came down, or, when that was off the video (the letterbox band
          // around it), where the finger is now, the first time it is over the video.
          const origin = this.isDragStartTried ? gesture.at : gesture.start;
          const pressure = this.isDragStartTried ? this.lastMovePressure : this.lastDownPressure;
          this.isDragStartTried = true;
          this.isTouchDragging = this.actions.touchBegin(origin.x, origin.y, pressure);
          if (!this.isTouchDragging) {
            this.setTransform(panBy(this.transform, gesture.delta, stageSize));
            return;
          }
          if (origin === gesture.at) return;
        }
        this.actions.touchMove(gesture.at.x, gesture.at.y, this.lastMovePressure);
        return;
      case "pan-end":
        this.isDragStartTried = false;
        if (this.isTouchDragging) {
          this.isTouchDragging = false;
          this.actions.touchEnd(gesture.at.x, gesture.at.y, this.lastMovePressure);
        }
        return;
      case "pinch":
        this.setTransform(
          zoomAbout(
            this.transform,
            gesture.factor,
            { x: gesture.about.x - rect.left, y: gesture.about.y - rect.top },
            stageSize,
          ),
        );
        return;
      case "scroll":
        if (this.isEnabled) this.actions.scroll(gesture.steps);
        return;
    }
  }

  private setTransform(transform: ViewTransform): void {
    this.transform = transform;
    this.video.style.transformOrigin = "0 0";
    this.video.style.transform = transformToCss(transform);
  }
}

function sampleOf(event: PointerEvent): PointerSample {
  return { pointerId: event.pointerId, x: event.clientX, y: event.clientY, timeMs: event.timeStamp };
}

/** A character as an X keysym: Latin-1 is its code point, anything else the Unicode keysym range. */
export function unicodeKeysym(character: string): number {
  const codePoint = character.codePointAt(0) ?? 0;
  if (codePoint === 0x0a) return XK_RETURN;
  if (codePoint >= 0x20 && codePoint <= 0xff) return codePoint;
  return 0x01000000 | codePoint;
}
