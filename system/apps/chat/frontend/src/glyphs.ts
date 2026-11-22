/**
 * The glyphs the chat app draws that the shared icon set (workspace-ui ``components/icons``) does not carry. Drawn
 * on the shared set's 24px grid with its round-capped stroke, so they sit beside its icons as one family.
 */

import m from "mithril";

function strokeGlyph(size: number, children: m.Children): m.Vnode {
  return m(
    "svg",
    {
      width: size,
      height: size,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    },
    children,
  );
}

export function plusGlyph(size = 16): m.Vnode {
  return strokeGlyph(size, [m("path", { d: "M12 5v14" }), m("path", { d: "M5 12h14" })]);
}

/** Three dots, filled rather than stroked: at 16px a stroked ring reads as a smudge. */
export function kebabGlyph(size = 16): m.Vnode {
  return strokeGlyph(
    size,
    [5, 12, 19].map((cy) => m("circle", { cx: 12, cy, r: 2, fill: "currentColor", stroke: "none" })),
  );
}

/** Two people, for a room: a chat among the workspace's people rather than with an agent. */
export function peopleGlyph(size = 14): m.Vnode {
  return strokeGlyph(size, [
    m("path", { d: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" }),
    m("circle", { cx: 9, cy: 7, r: 4 }),
    m("path", { d: "M22 21v-2a4 4 0 0 0-3-3.87" }),
    m("path", { d: "M16 3.13a4 4 0 0 1 0 7.75" }),
  ]);
}

export function listGlyph(size = 22): m.Vnode {
  return strokeGlyph(size, [m("path", { d: "M4 6h16" }), m("path", { d: "M4 12h16" }), m("path", { d: "M4 18h10" })]);
}

export function slidersGlyph(size = 20): m.Vnode {
  return strokeGlyph(
    size,
    ["M4 21v-7", "M4 10V3", "M12 21v-9", "M12 8V3", "M20 21v-5", "M20 12V3", "M1 14h6", "M9 8h6", "M17 16h6"].map(
      (d) => m("path", { d }),
    ),
  );
}
