import { TEXT_BODY_SIZE } from "./typography";

/* Input.
 * One text-field style for <input> / <textarea>. Options: mono (id/token-like
 * fields), withAction (reserves room for a trailing inline action button),
 * extra. The focus ring shows on click too: a field is always focused by
 * the keyboard's own reckoning. Genuinely special fields stay bespoke.
 *
 * A class builder, not a component, on purpose: the recipe spans two element
 * types that differ in their attribute surface, and there is no invariant a
 * wrapper would enforce -- a component would be an `as:` prop and ceremony.
 * Revisit if text fields multiply the way buttons did.
 *
 * The leading `input` class is a bare marker (no styling): a hook for tests,
 * for any contextual stylesheet rule, and for the inspector. The Tailwind
 * scanner reads utility names from the literals in this file: keep every
 * utility name a contiguous literal. */

export interface InputOptions {
  mono?: boolean;
  withAction?: boolean;
  extra?: string;
}

// The brand's field: 36px (an <input>; a <textarea> grows from 84px), 12px corners, a default edge that darkens
// under the pointer while the field rests, a 2px ring in the focus colour in place of the edge on focus, an error as
// the edge alone (aria-invalid), never a fill, and disabled as its own flat colours.
const INPUT_BASE =
  "input block w-full px-3 [&:is(input)]:h-9 [&:is(input)]:py-0 [&:is(textarea)]:min-h-21 [&:is(textarea)]:py-1.5 " +
  `${TEXT_BODY_SIZE} text-body-content placeholder:text-body-content-tertiary ` +
  "bg-body-surface-raised border border-default rounded-xl " +
  "transition-[border-color,outline-color] duration-(--dur-base) ease-[ease] " +
  "not-disabled:not-focus:not-aria-invalid:hover:border-default-hover " +
  "focus:border-transparent focus:outline-2 focus:outline-offset-0 focus:outline-focus " +
  "aria-invalid:border-danger-surface " +
  "disabled:bg-body-surface disabled:border-subtle disabled:text-body-content-tertiary disabled:cursor-not-allowed";

export function inputClass(options: InputOptions = {}): string {
  const { mono = false, withAction = false, extra = "" } = options;
  const parts = [INPUT_BASE];
  if (mono) parts.push("font-mono tracking-[0.01em]");
  if (withAction) parts.push("pr-16");
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}
