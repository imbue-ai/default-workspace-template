import { TEXT_BODY_SIZE } from "./typography";

/* ── Input ───────────────────────────────────────────────────────────────────
 * One text-field style for <input> / <textarea>. Options: mono (id/token-like
 * fields), withAction (reserves room for a trailing inline action button),
 * extra. Text fields are always focus-visible, so the accent ring also shows
 * on click. Genuinely special fields stay bespoke: the composer textbox, the
 * model-search field, and the inline tab-rename editor.
 *
 * A class builder, not a component, on purpose: the recipe spans two element
 * types (a census found 4 <input> + 1 <textarea> sites), the two differ in
 * their attribute surface, and there is no invariant a wrapper would enforce
 * -- so a component would be an `as:` prop and ceremony for five call sites.
 * Revisit if text fields multiply the way buttons did.
 *
 * The leading `input` class is a bare marker (no styling): a hook for tests,
 * for contextual stylesheet rules (e.g. `.claude-login-subtle-body .input`),
 * and for the inspector. The Tailwind scanner reads utility names from the
 * literals in this file: keep every utility name a contiguous literal. */

export interface InputOptions {
  mono?: boolean;
  withAction?: boolean;
  extra?: string;
}

const INPUT_BASE =
  "input block w-full px-3 py-2 " +
  `${TEXT_BODY_SIZE} text-primary placeholder:text-faint ` +
  "bg-surface border border-default rounded-md " +
  "transition-[border-color,box-shadow] duration-(--dur-base) ease-[ease] " +
  "focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
  "disabled:opacity-50 disabled:cursor-not-allowed";

export function inputClass(options: InputOptions = {}): string {
  const { mono = false, withAction = false, extra = "" } = options;
  const parts = [INPUT_BASE];
  if (mono) parts.push("font-mono tracking-[0.01em]");
  if (withAction) parts.push("pr-16");
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}
