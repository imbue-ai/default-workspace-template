import m from "mithril";
import { splitAttrs } from "./attrs";
import { TEXT_BODY_SIZE } from "./typography";

/* ── Button ──────────────────────────────────────────────────────────────────
 * One button system: the class recipe (buttonClass) and the component that
 * carries it (Button). Variants: primary | secondary | ghost | destructive |
 * ghost-destructive (quiet destructive: danger text, no fill) | inverse |
 * ghost-inverse (quiet on a dark overlay: light glyph, white-tint hover) |
 * stop (the composer's slate interrupt fill). Options: sm, icon (square),
 * round (circle), selected (accent-tint pressed look), block (full width),
 * extra (appended utilities/markers). States: hover (guarded so a disabled
 * button never tints), :focus-visible, :disabled + [aria-disabled], :active
 * press.
 *
 * `m(Button, {...})` is the default way to make a button: it renders a real
 * <button type="button"> and passes every attr it doesn't consume (onclick,
 * disabled, title, aria-*, data-*) through to the element. Use buttonClass()
 * directly only where a component can't go: the rare non-button element that
 * must read as a button (the login modal's OAuth link) or DOM built outside
 * mithril (the lightbox).
 *
 * The Tailwind scanner reads utility names from the literals in this file
 * (style.css's `@source` covers every .ts file): keep every utility name a
 * contiguous literal -- never build one by string interpolation. */

export type ButtonVariant =
  "primary" | "secondary" | "ghost" | "destructive" | "ghost-destructive" | "inverse" | "ghost-inverse" | "stop";

export interface ButtonOptions {
  sm?: boolean;
  icon?: boolean;
  round?: boolean;
  selected?: boolean;
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
  `${TEXT_BODY_SIZE} leading-none font-medium whitespace-nowrap cursor-pointer border ` +
  "transition-[color,background-color,border-color] duration-(--dur-base) ease-[ease] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
  "disabled:opacity-50 disabled:cursor-not-allowed aria-disabled:opacity-50 aria-disabled:cursor-not-allowed " +
  "not-disabled:not-aria-disabled:active:translate-y-px";

const BTN_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-accent text-on-accent border-accent not-disabled:hover:bg-accent-hover not-disabled:hover:border-accent-hover",
  secondary: "bg-surface text-primary border-default not-disabled:hover:bg-fill-hover",
  ghost:
    "bg-transparent text-secondary border-transparent not-disabled:hover:bg-fill-hover not-disabled:hover:text-primary",
  destructive:
    "bg-danger text-on-accent border-danger not-disabled:hover:bg-danger-hover not-disabled:hover:border-danger-hover",
  "ghost-destructive": "bg-transparent text-danger border-transparent not-disabled:hover:bg-danger-surface",
  inverse:
    "bg-inverse text-on-accent border-inverse not-disabled:hover:bg-inverse-hover not-disabled:hover:border-inverse-hover",
  // For controls sitting on a dark overlay (the lightbox). The whites are raw
  // (white/85, white/15) like the overlay's own black scrim -- there is no
  // dark-surface tint token, and on-accent covers only the full-strength hover.
  "ghost-inverse":
    "bg-transparent text-white/85 border-transparent not-disabled:hover:bg-white/15 not-disabled:hover:text-on-accent",
  stop: "bg-stop text-on-accent border-stop not-disabled:hover:bg-stop-hover not-disabled:hover:border-stop-hover",
};

// The selected (accent-tint) palette replaces the variant's colors outright --
// the builder resolves the conflict in code instead of leaning on the cascade.
const BTN_SELECTED = "bg-accent-light text-accent border-accent";

export function buttonClass(variant: ButtonVariant = "secondary", options: ButtonOptions = {}): string {
  const { sm = false, icon = false, round = false, selected = false, block = false, extra = "" } = options;
  const size = icon
    ? sm
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

const OWN_KEYS = ["variant", "sm", "icon", "round", "selected", "block", "extra"] as const;

export function Button(): m.Component<ButtonAttrs> {
  return {
    view(vnode) {
      const { variant = "secondary", sm, icon, round, selected, block, extra } = vnode.attrs;
      // The passthrough spread comes after `type`, so a caller can still opt
      // into type="submit" if a form ever appears.
      return m(
        "button",
        {
          type: "button",
          class: buttonClass(variant, { sm, icon, round, selected, block, extra }),
          ...splitAttrs(vnode.attrs, OWN_KEYS),
        },
        vnode.children,
      );
    },
  };
}
