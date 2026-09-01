import m from "mithril";
import { splitAttrs } from "./attrs";
import { TEXT_BODY_SIZE } from "./typography";

/* Button.
 * One button system: the class recipe (buttonClass) and the component that
 * carries it (Button).
 *
 * `m(Button, {...})` is the default way to make a button: it renders a real
 * <button type="button"> and passes every attr it doesn't consume (onclick,
 * disabled, title, aria-*, data-*) through to the element. Use buttonClass()
 * directly only where a component can't go: a non-button element that must
 * read as a button, or DOM built outside mithril.
 *
 * The Tailwind scanner reads utility names from the literals in this file
 * (style.css's `@source` covers every .ts file): keep every utility name a
 * contiguous literal -- never build one by string interpolation. */

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "ghost"
  | "destructive"
  | "ghost-destructive"
  | "inverse"
  | "ghost-inverse"
  | "stop";

export interface ButtonOptions {
  sm?: boolean;
  /** The smallest step, icon-only (20px box). Wins over `sm`; requires `icon`
   *  (there is no xs text button -- the recipe explodes if asked for one). */
  xs?: boolean;
  icon?: boolean;
  round?: boolean;
  /** Accent-tint pressed look; an icon ghost with `selected` is the on/off
   *  toggle recipe. */
  selected?: boolean;
  /** Non-interactive without `disabled`: resting look, default cursor, no
   *  hover/press. The element stays enabled so a hover tooltip can explain
   *  why; the component marks it aria-disabled for assistive tech. */
  readonly?: boolean;
  block?: boolean;
  extra?: string;
}

// No border-color or radius here: two utilities on the same property tie-break
// by their order in the COMPILED bundle, not by class order, so a base
// border-transparent would defeat every variant's border colour (and a base
// rounded-md would defeat the round option). Each property is emitted exactly
// once, resolved in the builder.
const BTN_BASE =
  "btn inline-flex items-center justify-center gap-1.5 " +
  `${TEXT_BODY_SIZE} leading-none font-medium whitespace-nowrap border ` +
  "transition-[color,background-color,border-color] duration-(--dur-base) ease-[ease] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
  "disabled:opacity-50 disabled:cursor-not-allowed " +
  "not-disabled:not-aria-disabled:active:translate-y-px";

// Cursor and the aria-disabled treatment, resolved in the builder: an
// interactive button dims when a caller marks it aria-disabled, but a readonly
// one keeps its resting look -- readonly IS the aria-disabled state, worn
// deliberately and explained by a tooltip, not a fault to grey out. The
// variants' hover tints are guarded off aria-disabled below, so marking the
// element silences them at runtime either way.
const BTN_INTERACTIVE = "cursor-pointer aria-disabled:opacity-50 aria-disabled:cursor-not-allowed";
const BTN_READONLY = "cursor-default";

const BTN_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-accent text-on-accent border-accent not-disabled:not-aria-disabled:hover:bg-accent-hover not-disabled:not-aria-disabled:hover:border-accent-hover",
  // The fill-tinted variants pair hover with the one-step-deeper pressed tint;
  // the solid variants keep their own hover colours and the shared translate-y
  // press cue instead.
  secondary:
    "bg-surface text-primary border-default not-disabled:not-aria-disabled:hover:bg-fill-hover not-disabled:not-aria-disabled:active:bg-fill-active",
  ghost:
    "bg-transparent text-secondary border-transparent not-disabled:not-aria-disabled:hover:bg-fill-hover not-disabled:not-aria-disabled:hover:text-primary not-disabled:not-aria-disabled:active:bg-fill-active",
  destructive:
    "bg-danger text-on-accent border-danger not-disabled:not-aria-disabled:hover:bg-danger-hover not-disabled:not-aria-disabled:hover:border-danger-hover",
  "ghost-destructive":
    "bg-transparent text-danger border-transparent not-disabled:not-aria-disabled:hover:bg-danger-surface",
  inverse:
    "bg-inverse text-on-accent border-inverse not-disabled:not-aria-disabled:hover:bg-inverse-hover not-disabled:not-aria-disabled:hover:border-inverse-hover",
  // For controls sitting on a dark overlay (the lightbox). The whites are raw
  // (white/85, white/15) like the overlay's own black scrim -- there is no
  // dark-surface tint token, and on-accent covers only the full-strength hover.
  "ghost-inverse":
    "bg-transparent text-white/85 border-transparent not-disabled:not-aria-disabled:hover:bg-white/15 not-disabled:not-aria-disabled:hover:text-on-accent",
  stop: "bg-stop text-on-accent border-stop not-disabled:not-aria-disabled:hover:bg-stop-hover not-disabled:not-aria-disabled:hover:border-stop-hover",
};

// The selected (accent-tint) palette replaces the variant's colors outright --
// the builder resolves the conflict in code instead of leaning on the cascade.
// Exported for the one decorative copy of a selected control (the fast-mode
// modal's inline illustration), so its tint cannot drift from the real thing.
// border-transparent, not border-accent: the tint alone says "on" -- an
// outline reads as a frame around the control (most visibly on the modal's
// inline illustration), and ghost <-> selected keeps identical geometry.
export const BTN_SELECTED = "bg-accent-light text-accent border-transparent";

export function buttonClass(variant: ButtonVariant = "secondary", options: ButtonOptions = {}): string {
  const {
    sm = false,
    xs = false,
    icon = false,
    round = false,
    selected = false,
    readonly = false,
    block = false,
    extra = "",
  } = options;
  if (xs && !icon) {
    throw new Error("buttonClass: xs is an icon-only size (pass icon: true)");
  }
  const size = icon
    ? xs
      ? "h-5 w-5 p-0"
      : sm
        ? "h-[28px] w-[28px] p-0"
        : "h-[34px] w-[34px] p-0"
    : sm
      ? "h-[28px] px-3"
      : "h-[34px] px-3.5";
  // `btn--<variant>` is a bare marker like `btn` (tests find "the primary
  // button" by it) -- interpolating it is fine because it is not a utility the
  // scanner needs to see.
  const parts = [
    BTN_BASE,
    readonly ? BTN_READONLY : BTN_INTERACTIVE,
    `btn--${variant}`,
    size,
    round ? "rounded-full" : "rounded-md",
    selected ? BTN_SELECTED : BTN_VARIANTS[variant],
  ];
  if (block) parts.push("w-full");
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}

interface ButtonAttrs extends m.Attributes, ButtonOptions {
  variant?: ButtonVariant;
}

const OWN_KEYS = ["variant", "sm", "xs", "icon", "round", "selected", "readonly", "block", "extra"] as const;

export function Button(): m.Component<ButtonAttrs> {
  return {
    view(vnode) {
      const { variant = "secondary", sm, xs, icon, round, selected, readonly, block, extra } = vnode.attrs;
      // The passthrough spread comes after `type` (so a caller can still opt
      // into type="submit" if a form ever appears) and after the readonly
      // aria-disabled default (so an explicit caller value wins).
      return m(
        "button",
        {
          type: "button",
          class: buttonClass(variant, { sm, xs, icon, round, selected, readonly, block, extra }),
          ...(readonly === true ? { "aria-disabled": "true" } : null),
          ...splitAttrs(vnode.attrs, OWN_KEYS),
        },
        vnode.children,
      );
    },
  };
}
