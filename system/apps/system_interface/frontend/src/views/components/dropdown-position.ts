/**
 * Pure horizontal placement for a composer popup (the model / effort pickers).
 *
 * The dropdown is anchored to its trigger's left edge (`left: 0` on the wrapper)
 * and nudged with a `translateX`. This module owns the one geometry decision --
 * where that left edge should sit -- kept free of the DOM so it is unit-testable;
 * ModelBar.ts measures the live rects and feeds them in.
 *
 * Two rules, in priority order:
 *   1. PRIMARY (hard): the whole dropdown stays inside the viewport with `margin`
 *      on both edges. Non-negotiable.
 *   2. SECONDARY (soft): its inner text lines up under the trigger's label text.
 *      Broken only as much as rule 1 requires.
 */

export interface DropdownClampInput {
  /** Viewport x of the trigger's label text (the model/effort name clicked). */
  labelLeft: number;
  /** Px from the dropdown's left border to its inner text (its own left padding),
   *  so aligning the *text* rather than the border lands the labels under the trigger. */
  textInset: number;
  /** Measured width of the dropdown box. */
  dropdownWidth: number;
  /** Current viewport width. */
  viewportWidth: number;
  /** Minimum gap to keep between the dropdown and each screen edge. */
  margin: number;
}

/**
 * The viewport-left the dropdown should occupy: aligned to the trigger label text
 * when there is room, otherwise clamped to keep the whole box on-screen.
 *
 * When the dropdown is too wide to fit within both margins (only possible at absurd
 * viewport widths, since CSS `max-width` bounds it below the viewport), rule 1
 * cannot be fully satisfied on both edges; we pin to the left margin so the header
 * and first characters stay visible and accept a hair of right overflow.
 */
export function clampDropdownLeft(input: DropdownClampInput): number {
  const { labelLeft, textInset, dropdownWidth, viewportWidth, margin } = input;
  const aligned = labelLeft - textInset;
  const maxLeft = viewportWidth - margin - dropdownWidth;
  const upper = Math.max(margin, maxLeft);
  return Math.min(Math.max(aligned, margin), upper);
}
