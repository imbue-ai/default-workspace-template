/**
 * The Foundations group: the colour tokens, the type roles and font stacks, spacing, radius, elevation, motion,
 * stacking and widths, and the icon set.
 *
 * Colours and the scales come from ``base.css`` itself (``models/tokens.ts``) and are drawn through inline
 * ``var(--...)`` styles, so a new token appears here unedited. The type roles are listed by hand: each is a
 * utility, and the Tailwind scanner only emits a utility it can read as a literal in the source.
 */

import m from "mithril";

import {
  alertDotIcon,
  icon,
  ICON_NAMES,
  spinnerIcon,
  statusDoneIcon,
  statusPendingIcon,
  statusRingIcon,
  stopIcon,
  warningIcon,
} from "@imbue/workspace-ui/src/components/icons";

import { tokensWithPrefix, type ColourToken, type DeclaredToken, type DesignTokens } from "../models/tokens";
import { caption, code, subCaption, type CatalogGroup, type CatalogSection } from "./catalog";

/** A type role: its utility (written out in full so the scanner sees it) and the tokens it bundles. */
interface TypeRole {
  readonly utility: string;
  readonly className: string;
  readonly sample: string;
  readonly sizeToken: string;
  readonly weightToken: string;
  readonly note?: string;
}

const TYPE_ROLES: readonly TypeRole[] = [
  {
    utility: "type-heading-lg",
    className: "type-heading-lg text-primary",
    sample: "Large heading",
    sizeToken: "font-size-heading-lg",
    weightToken: "weight-bold",
  },
  {
    utility: "type-heading",
    className: "type-heading text-primary",
    sample: "Regular heading",
    sizeToken: "font-size-heading",
    weightToken: "weight-semibold",
  },
  {
    utility: "type-label",
    className: "type-label text-primary",
    sample: "Label",
    sizeToken: "font-size-body",
    weightToken: "weight-semibold",
  },
  {
    utility: "type-body",
    className: "type-body text-primary",
    sample: "Body: the default readable paragraph text on a surface.",
    sizeToken: "font-size-body",
    weightToken: "weight-regular",
  },
  {
    utility: "type-helper",
    className: "type-helper text-primary",
    sample: "Helper: captions, hints and metadata sit at this size.",
    sizeToken: "font-size-helper",
    weightToken: "weight-regular",
  },
  {
    utility: "type-section",
    className: "type-section text-secondary",
    sample: "Section title",
    sizeToken: "font-size-helper",
    weightToken: "weight-semibold",
    note: "all caps, tracked",
  },
  {
    utility: "type-tag",
    className: "type-tag text-primary",
    sample: "A word badge such as Read-only",
    sizeToken: "font-size-tag",
    weightToken: "weight-regular",
  },
];

/** The spacing steps the style guide says are in use (``p-N`` / ``gap-N`` / ``m-N``, 4px a step). */
const SPACING_STEPS: readonly number[] = [0.5, 1, 1.5, 2, 3, 4, 5, 6, 8];

function valueIn(tokens: readonly DeclaredToken[], name: string): string {
  return tokens.find((token) => token.name === name)?.value ?? "?";
}

/** A theme token as a CSS value: the live variable, falling back to the declared value. Tailwind emits a theme
 *  variable only when a utility reads it through ``var()``, and some (the shadows) inline their value instead. */
function themeValue(token: DeclaredToken): string {
  return `var(--${token.name}, ${token.value})`;
}

function swatch(colour: ColourToken): m.Vnode {
  const paint = `var(--${colour.variable})`;
  const sample =
    colour.role === "text"
      ? m(
          "div",
          {
            // Text meant for a coloured surface ("on-accent") is shown on one.
            class: `flex h-12 items-center justify-center rounded-md border border-subtle type-heading ${
              colour.name.startsWith("on-") ? "bg-accent" : "bg-page"
            }`,
          },
          m("span", { style: { color: paint } }, "Aa"),
        )
      : colour.role === "border"
        ? m("div", { class: "h-12 rounded-md border-2 bg-surface", style: { borderColor: paint } })
        : m("div", { class: "h-12 rounded-md border border-subtle", style: { backgroundColor: paint } });
  return m("div", { id: `colour-${colour.name}`, class: "colour-swatch flex min-w-0 flex-col gap-1.5" }, [
    sample,
    m("div", [
      colour.utility === null ? caption(`--${colour.variable}`) : caption(colour.utility),
      colour.utility === null ? null : subCaption(`--${colour.variable}`),
      subCaption(colour.value),
    ]),
  ]);
}

function coloursSection(tokens: DesignTokens): CatalogSection {
  return {
    id: "colours",
    toc: "Colours",
    title: "Colour tokens",
    desc: [
      "Every colour has one semantic utility over one raw value. Views use the utility (",
      code("text-primary"),
      ", ",
      code("bg-fill-hover"),
      ", ",
      code("border-default"),
      "); hand-written CSS uses the ",
      code("--c-*"),
      " value. Each swatch is drawn with the live value, so this is the palette as it ships.",
    ],
    body: () =>
      tokens.colourGroups.map((group) =>
        m("div", { class: "colour-group mb-6", "data-colour-group": group.label }, [
          m("h3", { class: "type-section text-faint mb-2" }, group.label),
          m(
            "div",
            { class: "grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-x-4 gap-y-5" },
            group.colours.map(swatch),
          ),
        ]),
      ),
  };
}

function typeSection(tokens: DesignTokens): CatalogSection {
  return {
    id: "type",
    toc: "Type",
    title: "Type roles",
    desc: [
      "Seven roles, one utility each, bundling size, weight and line height. Colour is separate: compose a role with a text colour, e.g. ",
      code("type-label text-secondary"),
      ". Dense chrome (the rail, menus, launcher tables) sits on ",
      code("--font-size-row"),
      ".",
    ],
    body: () => [
      m(
        "div",
        { class: "flex flex-col gap-4" },
        TYPE_ROLES.map((role) =>
          m("div", { id: `type-${role.utility}`, class: "type-sample" }, [
            m("p", { class: role.className }, role.sample),
            caption(
              `${role.utility} -- ${valueIn(tokens.scales, role.sizeToken)} / ${valueIn(tokens.scales, role.weightToken)}` +
                (role.note === undefined ? "" : ` / ${role.note}`),
              "mt-0.5",
            ),
          ]),
        ),
      ),
      m("h3", { class: "type-section text-faint mt-8 mb-2" }, "Sizes and weights"),
      tokenTable(
        [...tokensWithPrefix(tokens.scales, "font-size-"), ...tokensWithPrefix(tokens.scales, "weight-")],
        (token) =>
          token.name.startsWith("font-size-")
            ? m("span", { style: { fontSize: `var(--${token.name})` } }, "The quick brown fox")
            : m("span", { style: { fontWeight: `var(--${token.name})` } }, "The quick brown fox"),
      ),
      m("h3", { class: "type-section text-faint mt-8 mb-2" }, "Font stacks"),
      m("div", { class: "flex flex-col gap-3" }, [
        m("div", { id: "font-sans" }, [
          m("p", { class: "font-sans type-body text-primary" }, "font-sans -- The workspace's interface text"),
          subCaption(valueIn(tokens.theme, "font-sans")),
        ]),
        m("div", { id: "font-mono" }, [
          m("p", { class: "font-mono type-body text-primary" }, "font-mono -- ids, paths, code"),
          subCaption(valueIn(tokens.theme, "font-mono")),
        ]),
      ]),
    ],
  };
}

/** A name / value / demo table over some tokens. */
function tokenTable(tokens: readonly DeclaredToken[], demo: (token: DeclaredToken) => m.Children): m.Vnode {
  return m(
    "div",
    { class: "token-table grid grid-cols-[minmax(160px,auto)_minmax(80px,auto)_1fr] items-center gap-x-6 gap-y-2" },
    tokens.flatMap((token) => [
      caption(`--${token.name}`),
      subCaption(token.value),
      m("div", { id: `token-${token.name}`, class: "min-w-0 type-body text-primary" }, demo(token)),
    ]),
  );
}

function spacingSection(tokens: DesignTokens): CatalogSection {
  return {
    id: "spacing",
    toc: "Spacing",
    title: "Spacing",
    desc: [
      "One knob, Tailwind's ",
      code("--spacing"),
      " base (",
      code(valueIn(tokens.theme, "spacing")),
      "), so ",
      code("p-N"),
      " / ",
      code("gap-N"),
      " / ",
      code("m-N"),
      " are N x 4px. These are the steps in use; an off-grid pixel value is a deliberate exception.",
    ],
    body: () =>
      m(
        "div",
        { class: "grid grid-cols-[64px_56px_1fr] items-center gap-x-4 gap-y-2" },
        SPACING_STEPS.flatMap((step) => [
          caption(`p-${step}`),
          subCaption(`${step * 4}px`),
          m("div", {
            id: `spacing-${step}`,
            class: "h-3 rounded-sm bg-accent",
            style: { width: `calc(var(--spacing) * ${step})` },
          }),
        ]),
      ),
  };
}

function radiusSection(tokens: DesignTokens): CatalogSection {
  return {
    id: "radius",
    toc: "Corner radius",
    title: "Corner radius",
    desc: [
      code("rounded-md"),
      " is the default control radius; ",
      code("rounded-lg"),
      " carries inputs, cards and modals; ",
      code("rounded-xl"),
      " the largest surfaces and the floating menus. Pills and circles are ",
      code("rounded-full"),
      ".",
    ],
    body: () =>
      m("div", { class: "flex flex-wrap items-end gap-5" }, [
        ...tokensWithPrefix(tokens.theme, "radius-").map((token) =>
          m("div", { id: `token-${token.name}`, class: "flex flex-col items-center gap-1.5" }, [
            m("div", {
              class: "h-16 w-16 border border-default bg-surface-secondary",
              style: { borderRadius: themeValue(token) },
            }),
            caption(`rounded-${token.name.slice("radius-".length)}`),
            subCaption(token.value),
          ]),
        ),
        m("div", { id: "token-radius-full", class: "flex flex-col items-center gap-1.5" }, [
          m("div", { class: "h-16 w-16 rounded-full border border-default bg-surface-secondary" }),
          caption("rounded-full"),
          subCaption("9999px"),
        ]),
      ]),
  };
}

function elevationSection(tokens: DesignTokens): CatalogSection {
  return {
    id: "elevation",
    toc: "Elevation",
    title: "Elevation",
    desc: [
      code("shadow-raised"),
      " is the subtle resting lift on interactive cards; ",
      code("shadow-overlay"),
      " the one soft floating shadow for everything above the page: menus, pickers, modals, tooltips.",
    ],
    body: () =>
      m(
        "div",
        { class: "flex flex-wrap gap-8 rounded-lg bg-page p-6" },
        tokensWithPrefix(tokens.theme, "shadow-").map((token) =>
          m("div", { id: `token-${token.name}`, class: "flex flex-col gap-2" }, [
            m("div", {
              class: "h-20 w-40 rounded-lg border border-default bg-surface",
              style: { boxShadow: themeValue(token) },
            }),
            caption(token.name),
          ]),
        ),
      ),
  };
}

function motionSection(tokens: DesignTokens): CatalogSection {
  return {
    id: "motion",
    toc: "Motion",
    title: "Motion",
    desc: [
      "Three durations back the common transitions (",
      code("duration-(--dur-base)"),
      "). Hover a track to play it.",
    ],
    body: () =>
      m(
        "div",
        { class: "flex flex-col gap-3" },
        tokensWithPrefix(tokens.scales, "dur-").map((token) =>
          m("div", { id: `token-${token.name}`, class: "group flex items-center gap-4" }, [
            m("div", { class: "w-28" }, [caption(`--${token.name}`), subCaption(token.value)]),
            m(
              "div",
              { class: "relative h-6 w-64 rounded-full bg-surface-secondary" },
              m("div", {
                class:
                  "absolute top-1 left-1 h-4 w-4 rounded-full bg-accent transition-transform ease-[ease] group-hover:translate-x-[232px]",
                style: { transitionDuration: `var(--${token.name})` },
              }),
            ),
          ]),
        ),
      ),
  };
}

function layersSection(tokens: DesignTokens): CatalogSection {
  return {
    id: "layers",
    toc: "Layers & widths",
    title: "Stacking layers and widths",
    desc: [
      "The named stacking layers (",
      code("z-(--z-overlay)"),
      "): modal backdrops at overlay, menus and dropdowns above them at popover, the hover tooltip over everything. And the fixed widths the chat's column is laid out on.",
    ],
    body: () =>
      tokenTable([...tokensWithPrefix(tokens.scales, "z-"), ...tokensWithPrefix(tokens.scales, "width-")], (token) =>
        token.name.startsWith("width-")
          ? m("div", {
              class: "h-2 max-w-full rounded-sm bg-faint",
              style: { width: `calc(var(--${token.name}) / 3)` },
            })
          : null,
      ),
  };
}

function iconsSection(): CatalogSection {
  const special: readonly [string, string][] = [
    ["stopIcon", stopIcon(16)],
    ["warningIcon", warningIcon(20)],
    ["alertDotIcon", alertDotIcon(14, "text-danger")],
    ["spinnerIcon", spinnerIcon(18)],
    ["statusDoneIcon", statusDoneIcon()],
    ["statusRingIcon", statusRingIcon()],
    ["statusPendingIcon", statusPendingIcon()],
  ];
  return {
    id: "icons",
    toc: "Icons",
    title: "Icons",
    desc: [
      "One stroke-outline set on a 24px grid, drawn in ",
      code("currentColor"),
      " by ",
      code("icon(name, { size })"),
      ", plus the filled and status glyphs that have builders of their own.",
    ],
    body: () => [
      m(
        "div",
        { class: "grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-x-4 gap-y-3" },
        ICON_NAMES.map((name) =>
          m("div", { id: `icon-${name}`, class: "icon-tile flex items-center gap-2.5 text-primary" }, [
            m.trust(icon(name, { size: 18 })),
            caption(name),
          ]),
        ),
      ),
      m("h3", { class: "type-section text-faint mt-6 mb-2" }, "Glyphs with their own builders"),
      m(
        "div",
        { class: "grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-x-4 gap-y-3" },
        special.map(([name, markup]) =>
          m("div", { id: `icon-${name}`, class: "icon-tile flex items-center gap-2.5 text-primary" }, [
            m.trust(markup),
            caption(name),
          ]),
        ),
      ),
    ],
  };
}

export function foundationsGroup(tokens: DesignTokens): CatalogGroup {
  return {
    id: "foundations",
    label: "Foundations",
    description: "The tokens every surface is built from, read live from the shared stylesheet, and the icon set.",
    sections: [
      coloursSection(tokens),
      typeSection(tokens),
      spacingSection(tokens),
      radiusSection(tokens),
      elevationSection(tokens),
      motionSection(tokens),
      layersSection(tokens),
      iconsSection(),
    ],
  };
}
