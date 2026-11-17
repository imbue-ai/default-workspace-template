import m from "mithril";
import { icon } from "./icons";
import { CHECKBOX_PART, SWITCH_PART } from "../themes/parts";

/* Form controls: the checkbox, the switch, a field's label, and its error line, in the brand's shapes
 * (docs/system/blueprint/brand-design-system/). The text field is `inputClass` (Input.ts) and the value picker is
 * `Dropdown` (dropdown.ts).
 *
 * "On" is the primary role -- the workspace color -- with the content that reads on it: the brand keeps the accent
 * for actions that carry the brand. The Tailwind scanner reads utility names from the literals in this file: keep
 * every utility name a contiguous literal. */

// A field's name above it: a static label, so the brand's mono label.
export const FORM_LABEL_CLASS = "form-label mb-1.5 block type-label text-body-content-secondary";

// What is wrong with a field, under it. The field itself says so with its edge (aria-invalid), never a fill.
export const FIELD_ERROR_CLASS = "field-error mt-1.5 type-helper text-danger-surface";

// Checkbox: 16px with 4px corners. The real <input> lies over the drawn box, so the keyboard, a click, and a
// <label> around it all work as they do on any checkbox; the tick is drawn over it and lets clicks through.
const CHECKBOX_BOX_CLASS = "checkbox relative inline-flex h-4 w-4 shrink-0 items-center justify-center";
const CHECKBOX_INPUT_CLASS =
  "peer absolute inset-0 m-0 h-4 w-4 cursor-pointer appearance-none rounded-sm border border-neutral-7 " +
  "bg-body-surface-raised transition-colors duration-(--dur-fast) " +
  "checked:border-primary-surface checked:bg-primary-surface " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus " +
  "disabled:cursor-not-allowed disabled:border-subtle disabled:bg-body-surface " +
  "disabled:checked:border-fill-active disabled:checked:bg-fill-active";
const CHECKBOX_TICK_CLASS =
  "pointer-events-none relative hidden text-primary-content peer-checked:flex peer-disabled:text-body-content-tertiary";

export interface CheckboxAttrs {
  readonly checked: boolean;
  readonly onchange: (checked: boolean) => void;
  readonly disabled?: boolean;
  /** For a checkbox with no <label> around it. */
  readonly "aria-label"?: string;
  /** Attributes for the <input> itself (a test hook such as `data-*`). */
  readonly input?: m.Attributes;
}

export const Checkbox: m.Component<CheckboxAttrs> = {
  view(vnode) {
    const { checked, onchange, disabled = false, input = {} } = vnode.attrs;
    return m("span", { class: CHECKBOX_BOX_CLASS, ...CHECKBOX_PART }, [
      m("input", {
        type: "checkbox",
        class: CHECKBOX_INPUT_CLASS,
        checked,
        disabled,
        "aria-label": vnode.attrs["aria-label"],
        onchange: (event: Event) => onchange((event.target as HTMLInputElement).checked),
        ...input,
      }),
      m("span", { class: CHECKBOX_TICK_CLASS }, m.trust(icon("check", { size: 12, strokeWidth: 2.5 }))),
    ]);
  },
};

// Switch: a 32 by 18 track with a 14px knob 2px in from either end; color and the knob's place say on or off.
const SWITCH_TRACK_CLASS =
  "switch-track relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-full transition-colors duration-(--dur-base)";
const SWITCH_TRACK_ON_CLASS = "bg-primary-surface";
const SWITCH_TRACK_OFF_CLASS = "bg-neutral-5";
const SWITCH_KNOB_CLASS =
  "switch-knob inline-flex h-3.5 w-3.5 rounded-full transition-transform duration-(--dur-base)";
const SWITCH_KNOB_ON_CLASS = "translate-x-4 bg-primary-content";
const SWITCH_KNOB_OFF_CLASS = "translate-x-0.5 bg-body-surface-raised";

/** The switch's drawing alone, for a control that is a switch as a whole (a menu row, a labelled toggle) and so
 *  carries `role="switch"` and `aria-checked` itself. */
export function switchVisual(on: boolean): m.Vnode {
  return m(
    "span",
    {
      class: `${SWITCH_TRACK_CLASS} ${on ? SWITCH_TRACK_ON_CLASS : SWITCH_TRACK_OFF_CLASS}`,
      "aria-hidden": "true",
      ...SWITCH_PART,
      "data-on": on ? "true" : "false",
    },
    m("span", { class: `${SWITCH_KNOB_CLASS} ${on ? SWITCH_KNOB_ON_CLASS : SWITCH_KNOB_OFF_CLASS}` }),
  );
}

export interface SwitchAttrs {
  readonly on: boolean;
  readonly onToggle: () => void;
  /** Shown beside the switch, and its accessible name. */
  readonly label?: string;
  /** The accessible name of a switch shown without a label. */
  readonly "aria-label"?: string;
  readonly disabled?: boolean;
  /** Classes for the whole control. */
  readonly extra?: string;
}

/** A switch: a button with `role="switch"`, its label (when it has one) before the drawing. */
export const Switch: m.Component<SwitchAttrs> = {
  view(vnode) {
    const { on, onToggle, label, disabled = false, extra = "" } = vnode.attrs;
    return m(
      "button",
      {
        type: "button",
        role: "switch",
        "aria-checked": on ? "true" : "false",
        "aria-label": label === undefined ? vnode.attrs["aria-label"] : undefined,
        disabled,
        class:
          "switch inline-flex cursor-pointer items-center gap-2 rounded-md text-body-content " +
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus " +
          "disabled:cursor-not-allowed disabled:text-body-content-tertiary" +
          (extra === "" ? "" : ` ${extra}`),
        onclick: () => onToggle(),
      },
      [label === undefined ? null : m("span", label), switchVisual(on)],
    );
  },
};
