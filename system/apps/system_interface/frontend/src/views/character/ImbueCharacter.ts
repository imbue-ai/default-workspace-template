/**
 * The imbue character: one filled SVG path, no outline, no face -- the way the
 * imbue mark is drawn. The character is the motion.
 *
 * Unlike every other avatar design, this one is not a drawing the shell serves
 * as an image; it is a rig running in this page. That is the whole reason it
 * can be poked. The passive designs stay passive -- see `AvatarImage`, which
 * picks between the two.
 *
 * Mithril renders this once. After that the rig's own loop owns the path and
 * the transforms, and a redraw only happens when something changes at human
 * speed: the mood, the size, whether the user is here.
 */

import m from "mithril";
import { type CharacterElements, driveCharacter, paintAtRest } from "./characterView";
import { applyMood, type CharacterMood, posture, press, releasePress } from "./poses";
import { type BlobRig, createBlobRig } from "./rig";
import { CHARACTER_COLOR } from "./stillFrame";

/** The catalog id the workspace answers with this component instead of an image.
 *  Matches `LIVE_DESIGN_ID` in the shell's avatar package. */
export const IMBUE_CHARACTER_DESIGN_ID = "imbue-character";

/** Rest radius in viewBox units. The viewBox is tight to the resting body;
 *  motion spills past it and the svg is set to let it. */
const R = 100;

/**
 * Where the floor is, and the pool of shade on it.
 *
 * The floor sits a little below the body rather than against it: a shadow in
 * contact says the character is resting *on* the surface, and a visible gap is
 * the whole difference between sitting and hovering. The pool is drawn as a
 * radial gradient rather than a blurred ellipse because an SVG filter has a
 * clipping region to get wrong and this has none, and because scaling a
 * gradient softens its edge in proportion -- which is exactly what a shadow
 * spreading as it rises should do.
 */
const FLOOR = R * 1.22;
const SHADOW_RX = R * 0.7;
const SHADOW_RY = R * 0.17;
/** Warm near-black, and the alpha at the middle of the pool. */
const SHADOW_INK = "#20100A";
const SHADOW_ALPHA = 0.44;

/** Per-mount, so two characters on one page never share a gradient. */
let gradientSerial = 0;

export interface ImbueCharacterAttrs {
  /**
   * The character's width in px at rest -- not the element's box, which is the
   * same size, since motion spills past that box rather than being fitted
   * inside it. Omitted where a class sizes the element instead, which is how
   * the taskbar entries do it.
   */
  readonly size?: number;
  /** Fill for the body. */
  readonly color?: string;
  /** What it is doing; the rig holds a pose per mood. */
  readonly mood: CharacterMood;
  /** Whether the user is here: its window is on screen and focused. */
  readonly isAttending?: boolean;
  /** Soft pool on the surface below, so the character reads as sitting on one. */
  readonly shadow?: boolean;
  /** Whether a press dents it. Off where the character is decoration. */
  readonly interactive?: boolean;
  readonly class?: string;
}

export function ImbueCharacter(): m.Component<ImbueCharacterAttrs> {
  const rig: BlobRig = createBlobRig({ radius: R });
  const inkId = `imbue-character-shade-${(gradientSerial += 1)}`;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  let stop: (() => void) | null = null;
  let shownMood: CharacterMood | null = null;
  let wasAttending = false;
  let pressedPointer: number | null = null;

  function elementsOf(root: SVGSVGElement): CharacterElements {
    const group = root.querySelector<SVGGElement>("[data-character-body]");
    const path = root.querySelector<SVGPathElement>("[data-character-path]");
    if (group === null || path === null) throw new Error("the character is missing its body");
    return {
      group,
      path,
      shadow: root.querySelector<SVGEllipseElement>("[data-character-shadow]"),
    };
  }

  /** Start (or restart) whichever of the two draw paths the motion preference
   *  asks for. Called again whenever the element set changes underneath it. */
  function draw(root: SVGSVGElement): void {
    stop?.();
    stop = null;
    const elements = elementsOf(root);
    if (reducedMotion.matches) {
      // Run forward and then hold, rather than turning the idle off: the
      // wander is what gives the resting body its shape, and a rig with it
      // disabled relaxes to the bare oval underneath. Reduced motion asks for
      // no movement, not for a different character.
      paintAtRest(rig, elements, (settled) => posture(settled, wasAttending));
      return;
    }
    stop = driveCharacter(rig, elements);
  }

  /** The press angle, in radians from the body's rest centre. */
  function angleOf(event: PointerEvent, root: SVGSVGElement): number {
    const box = root.getBoundingClientRect();
    if (box.width === 0) return 0;
    const scale = (2 * R) / box.width;
    const x = (event.clientX - box.left - box.width / 2) * scale;
    const y = (event.clientY - box.top - box.height / 2) * scale;
    return Math.atan2(y, x);
  }

  function endPress(pointerId: number): void {
    if (pressedPointer !== pointerId) return;
    pressedPointer = null;
    releasePress(rig);
  }

  let onMotionPreferenceChange: (() => void) | null = null;

  return {
    oncreate(vnode) {
      const root = vnode.dom as SVGSVGElement;
      wasAttending = vnode.attrs.isAttending === true;
      shownMood = vnode.attrs.mood;
      applyMood(rig, shownMood, wasAttending);
      draw(root);
      onMotionPreferenceChange = () => draw(root);
      reducedMotion.addEventListener("change", onMotionPreferenceChange);
    },

    onupdate(vnode) {
      const isAttending = vnode.attrs.isAttending === true;
      if (vnode.attrs.mood !== shownMood) {
        shownMood = vnode.attrs.mood;
        applyMood(rig, shownMood, isAttending);
      }
      if (isAttending !== wasAttending) {
        posture(rig, isAttending);
        wasAttending = isAttending;
      }
    },

    onremove() {
      stop?.();
      stop = null;
      if (onMotionPreferenceChange !== null) {
        reducedMotion.removeEventListener("change", onMotionPreferenceChange);
        onMotionPreferenceChange = null;
      }
    },

    view(vnode) {
      const { size, color = CHARACTER_COLOR, shadow = true, interactive = true } = vnode.attrs;
      return m(
        "svg",
        {
          width: size,
          height: size,
          viewBox: `${-R} ${-R} ${R * 2} ${R * 2}`,
          style: { overflow: "visible", touchAction: "none", color },
          role: "img",
          "aria-label": "imbue character",
          class: vnode.attrs.class,
          onpointerdown: (event: PointerEvent) => {
            if (!interactive || reducedMotion.matches) return;
            pressedPointer = event.pointerId;
            press(rig, angleOf(event, event.currentTarget as SVGSVGElement));
          },
          // Up, cancel, and leave all end the press. Leaving counts because
          // nothing captures the pointer, so a release off the element never
          // arrives here and the body would stay squashed.
          onpointerup: (event: PointerEvent) => endPress(event.pointerId),
          onpointercancel: (event: PointerEvent) => endPress(event.pointerId),
          onpointerleave: (event: PointerEvent) => endPress(event.pointerId),
        },
        [
          shadow
            ? m("g", [
                m(
                  "defs",
                  m("radialGradient", { id: inkId }, [
                    m("stop", { offset: "0", "stop-color": SHADOW_INK, "stop-opacity": SHADOW_ALPHA }),
                    m("stop", { offset: "0.45", "stop-color": SHADOW_INK, "stop-opacity": SHADOW_ALPHA * 0.62 }),
                    m("stop", { offset: "1", "stop-color": SHADOW_INK, "stop-opacity": 0 }),
                  ]),
                ),
                m(
                  "g",
                  { transform: `translate(0 ${FLOOR})`, "pointer-events": "none" },
                  m("ellipse", {
                    "data-character-shadow": "true",
                    rx: SHADOW_RX,
                    ry: SHADOW_RY,
                    fill: `url(#${inkId})`,
                  }),
                ),
              ])
            : null,
          m("g", { "data-character-body": "true" }, m("path", { "data-character-path": "true", d: "", fill: color })),
        ],
      );
    },
  };
}
