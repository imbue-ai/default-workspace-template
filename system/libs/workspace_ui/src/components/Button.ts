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
 * (base.css's `@source` covers every .ts file): keep every utility name a
 * contiguous literal -- never build one by string interpolation. */

export type ButtonVariant =
  | "primary"
  | "accent"
  | "secondary"
  | "ghost"
  | "destructive"
  | "ghost-destructive"
  | "inverse"
  | "ghost-inverse"
  | "stop";

export interface ButtonOptions {
  sm?: boolean;
  /** The large step (36px), for the one action a page or a dialog is for. Loses to `sm`. */
  lg?: boolean;
  /** The smallest step, icon-only (20px box). Wins over `sm`; requires `icon`
   *  (there is no xs text button -- the recipe explodes if asked for one). */
  xs?: boolean;
  icon?: boolean;
  round?: boolean;
  /** The held, active-fill look; an icon ghost with `selected` is the on/off
   *  toggle recipe. */
  selected?: boolean;
  /** Non-interactive without `disabled`: resting look, default cursor, no
   *  hover/press. The element stays enabled so a hover tooltip can explain
   *  why; the component marks it aria-disabled for assistive tech. */
  readonly?: boolean;
  /** Regular text weight, for a button that should read as quiet inline text
   *  among other text (the composer under-bar's actions, next to the model
   *  bar's regular-weight triggers) rather than as a labeled control. */
  quiet?: boolean;
  block?: boolean;
  extra?: string;
}

// No border-color, radius, or font weight here: two utilities on the same
// property tie-break by their order in the COMPILED bundle, not by class
// order, so a base border-transparent would defeat every variant's border
// colour (a base rounded-md the round option, a base font-medium the quiet
// option). Each property is emitted exactly once, resolved in the builder.
const BTN_BASE =
  "btn inline-flex items-center justify-center gap-1.5 " +
  `${TEXT_BODY_SIZE} leading-none whitespace-nowrap border ` +
  "transition-[color,background-color,border-color] duration-(--dur-base) ease-[ease] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus " +
  // Disabled is its own colors, not a fade: placeholder text, and each variant's own flat fill below.
  "disabled:text-body-content-tertiary disabled:cursor-not-allowed " +
  "not-disabled:not-aria-disabled:active:translate-y-px";

// Cursor and the aria-disabled treatment, resolved in the builder: an
// interactive button dims when a caller marks it aria-disabled, but a readonly
// one keeps its resting look -- readonly IS the aria-disabled state, worn
// deliberately and explained by a tooltip, not a fault to grey out. The
// variants' hover tints are guarded off aria-disabled below, so marking the
// element silences them at runtime either way.
const BTN_INTERACTIVE = "cursor-pointer aria-disabled:opacity-40 aria-disabled:cursor-not-allowed";
const BTN_READONLY = "cursor-default";

// A solid variant's disabled look: the active fill, no edge.
const BTN_SOLID_DISABLED = "disabled:bg-fill-active disabled:border-transparent";

const BTN_VARIANTS: Record<ButtonVariant, string> = {
  // The main action wears the workspace color: the primary role, the same color as the window around the
  // workspace in Imbue Studio. A dialog's confirm is primary.
  primary:
    "bg-primary-surface text-primary-content border-primary-surface not-disabled:not-aria-disabled:hover:bg-primary-surface-hover not-disabled:not-aria-disabled:hover:border-primary-surface-hover " +
    BTN_SOLID_DISABLED,
  // An action that carries the brand (send, make it mine). Accent stays rare.
  accent:
    "bg-accent-surface text-accent-content border-accent-surface not-disabled:not-aria-disabled:hover:bg-accent-surface-hover not-disabled:not-aria-disabled:hover:border-accent-surface-hover " +
    BTN_SOLID_DISABLED,
  // The fill-tinted variants pair hover with the one-step-deeper pressed tint;
  // the solid variants keep their own hover colours and the shared translate-y
  // press cue instead.
  secondary:
    "bg-body-surface-raised text-body-content border-default not-disabled:not-aria-disabled:hover:bg-fill-hover not-disabled:not-aria-disabled:active:bg-fill-active disabled:bg-body-surface disabled:border-subtle",
  ghost:
    "bg-transparent text-body-content-secondary border-transparent not-disabled:not-aria-disabled:hover:bg-fill-hover not-disabled:not-aria-disabled:hover:text-body-content not-disabled:not-aria-disabled:active:bg-fill-active",
  destructive:
    "bg-danger-surface text-danger-content border-danger-surface not-disabled:not-aria-disabled:hover:bg-danger-surface-hover not-disabled:not-aria-disabled:hover:border-danger-surface-hover " +
    BTN_SOLID_DISABLED,
  "ghost-destructive":
    "bg-transparent text-danger-surface border-transparent not-disabled:not-aria-disabled:hover:bg-danger-subtle-surface",
  inverse:
    "bg-inverse-surface text-inverse-content border-inverse-surface not-disabled:not-aria-disabled:hover:bg-inverse-surface-hover not-disabled:not-aria-disabled:hover:border-inverse-surface-hover " +
    BTN_SOLID_DISABLED,
  // For controls sitting on a dark overlay (the lightbox). The whites are raw
  // (white/85, white/15) like the overlay's own black scrim -- there is no
  // dark-surface tint token, and on-accent covers only the full-strength hover.
  "ghost-inverse":
    "bg-transparent text-white/85 border-transparent not-disabled:not-aria-disabled:hover:bg-white/15 not-disabled:not-aria-disabled:hover:text-inverse-content",
  stop:
    "bg-inverse-surface text-inverse-content border-inverse-surface not-disabled:not-aria-disabled:hover:bg-inverse-surface-hover not-disabled:not-aria-disabled:hover:border-inverse-surface-hover " +
    BTN_SOLID_DISABLED,
};

// The selected palette replaces the variant's colors outright -- the builder
// resolves the conflict in code instead of leaning on the cascade. It is the
// active fill in body text: the brand keeps the accent rare, so "on" is a
// neutral tint. Exported for the one decorative copy of a selected control (the
// fast-mode modal's inline illustration), so its tint cannot drift from the
// real thing. No edge: the tint alone says "on", and ghost <-> selected keeps
// identical geometry.
export const BTN_SELECTED = "bg-fill-active text-body-content border-transparent";

export function buttonClass(variant: ButtonVariant = "secondary", options: ButtonOptions = {}): string {
  const {
    sm = false,
    lg = false,
    xs = false,
    icon = false,
    round = false,
    selected = false,
    readonly = false,
    quiet = false,
    block = false,
    extra = "",
  } = options;
  if (xs && !icon) {
    throw new Error("buttonClass: xs is an icon-only size (pass icon: true)");
  }
  // The brand's steps: 28 (sm), 32, 36 (lg), and a square of the same height for an icon. A leading icon pulls
  // the side next to it in.
  const size = icon
    ? xs
      ? "h-5 w-5 p-0"
      : sm
        ? "h-7 w-7 p-0"
        : lg
          ? "h-9 w-9 p-0"
          : "h-8 w-8 p-0"
    : sm
      ? "h-7 px-2 has-[>svg:first-child]:pl-1.5"
      : lg
        ? "h-9 px-4 has-[>svg:first-child]:pl-3"
        : "h-8 px-3 has-[>svg:first-child]:pl-2";
  // `btn--<variant>` is a bare marker like `btn` (tests find "the primary
  // button" by it) -- interpolating it is fine because it is not a utility the
  // scanner needs to see.
  const parts = [
    BTN_BASE,
    readonly ? BTN_READONLY : BTN_INTERACTIVE,
    `btn--${variant}`,
    size,
    quiet ? "font-normal" : "font-medium",
    round ? "rounded-full" : "rounded-xl",
    selected ? BTN_SELECTED : BTN_VARIANTS[variant],
  ];
  if (block) parts.push("w-full");
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}

/** The contract part a button is (docs/system/blueprint/workspace-themes/, section 4.2), for an element that
 *  takes `buttonClass()` without being a `Button`. */
export function buttonPartAttrs(
  variant: ButtonVariant,
  isIcon: boolean,
): { readonly "data-part": "button"; readonly "data-variant": ButtonVariant; readonly "data-shape": "icon" | "text" } {
  return { "data-part": "button", "data-variant": variant, "data-shape": isIcon ? "icon" : "text" };
}

interface ButtonAttrs extends m.Attributes, ButtonOptions {
  variant?: ButtonVariant;
}

const OWN_KEYS = [
  "variant",
  "sm",
  "lg",
  "xs",
  "icon",
  "round",
  "selected",
  "readonly",
  "quiet",
  "block",
  "extra",
] as const;

export function Button(): m.Component<ButtonAttrs> {
  return {
    view(vnode) {
      const { variant = "secondary", sm, lg, xs, icon, round, selected, readonly, quiet, block, extra } = vnode.attrs;
      // The passthrough spread comes after `type` (so a caller can still opt
      // into type="submit" if a form ever appears) and after the readonly
      // aria-disabled default (so an explicit caller value wins).
      return m(
        "button",
        {
          type: "button",
          class: buttonClass(variant, { sm, lg, xs, icon, round, selected, readonly, quiet, block, extra }),
          ...buttonPartAttrs(variant, icon === true),
          ...(readonly === true ? { "aria-disabled": "true" } : null),
          ...splitAttrs(vnode.attrs, OWN_KEYS),
        },
        vnode.children,
      );
    },
  };
}
