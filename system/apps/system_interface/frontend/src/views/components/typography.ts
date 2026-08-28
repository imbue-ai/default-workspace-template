// Type-size fragments shared by the primitive recipes (Button, Input, Badge,
// Modal). Sizes reference the --font-size-* role tokens (see style.css) rather
// than Tailwind's text-* steps, so the type scale stays the single source of
// truth. For an off-role size at an ordinary call site, prefer a type-* role
// utility first (see the style guide).
export const TEXT_BODY_SIZE = "text-(length:--font-size-body)";
export const TEXT_HELPER_SIZE = "text-(length:--font-size-helper)";
export const TEXT_HEADING_SIZE = "text-(length:--font-size-heading)";
