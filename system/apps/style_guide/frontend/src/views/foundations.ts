/**
 * The Foundations group: the design tokens and the icon set.
 *
 * Colors and the scales come from ``base.css`` itself (``models/tokens.ts``) and are drawn through inline
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
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { FIELD_PART } from "@imbue/workspace-ui/src/themes/parts";

import { tokensWithPrefix, type ColorToken, type DeclaredToken, type DesignTokens } from "../models/tokens";
import { themeValue } from "../models/themeValues";
import {
  STUDIO_PALETTE,
  canonicalHex,
  previewWorkspaceColor,
  previewedWorkspaceColor,
  stopPreviewingWorkspaceColor,
} from "../models/workspaceColorPreview";
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
    className: "type-heading-lg text-body-content",
    sample: "Large heading",
    sizeToken: "font-size-heading-lg",
    weightToken: "weight-regular",
    note: "Besley: the one headline a window exists to show",
  },
  {
    utility: "type-heading",
    className: "type-heading text-body-content",
    sample: "Regular heading",
    sizeToken: "font-size-heading",
    weightToken: "weight-regular",
    note: "Besley",
  },
  {
    utility: "type-title-page",
    className: "type-title-page text-body-content",
    sample: "Page title",
    sizeToken: "font-size-title-page",
    weightToken: "weight-bold",
  },
  {
    utility: "type-title",
    className: "type-title text-body-content",
    sample: "Title",
    sizeToken: "font-size-title",
    weightToken: "weight-bold",
  },
  {
    utility: "type-title-sm",
    className: "type-title-sm text-body-content",
    sample: "The title of a card, a request, or a row",
    sizeToken: "font-size-body",
    weightToken: "weight-bold",
  },
  {
    utility: "type-body",
    className: "type-body text-body-content",
    sample: "Body: the default readable paragraph text on a surface.",
    sizeToken: "font-size-body",
    weightToken: "weight-regular",
  },
  {
    utility: "type-body-dense",
    className: "type-body-dense text-body-content",
    sample: "Dense body: menus, segmented controls, list rows.",
    sizeToken: "font-size-body-dense",
    weightToken: "weight-regular",
  },
  {
    utility: "type-label",
    className: "type-label text-body-content",
    sample: "Static label",
    sizeToken: "font-size-body-dense",
    weightToken: "weight-medium",
    note: "mono",
  },
  {
    utility: "type-label-large",
    className: "type-label-large text-body-content",
    sample: "Large static label",
    sizeToken: "font-size-body",
    weightToken: "weight-medium",
    note: "mono",
  },
  {
    utility: "type-helper",
    className: "type-helper text-body-content",
    sample: "Helper: captions, hints and metadata sit at this size.",
    sizeToken: "font-size-helper",
    weightToken: "weight-regular",
  },
  {
    utility: "type-section",
    className: "type-section text-body-content-secondary",
    sample: "Section title",
    sizeToken: "font-size-section",
    weightToken: "weight-medium",
    note: "all caps, tracked",
  },
  {
    utility: "type-code",
    className: "type-code text-body-content",
    sample: "const answer = 42;",
    sizeToken: "font-size-body-dense",
    weightToken: "weight-regular",
    note: "mono",
  },
  {
    utility: "type-code-small",
    className: "type-code-small text-body-content",
    sample: "const answer = 42;",
    sizeToken: "font-size-helper",
    weightToken: "weight-regular",
    note: "mono",
  },
  {
    utility: "type-tag",
    className: "type-tag text-body-content",
    sample: "A word badge such as Read-only",
    sizeToken: "font-size-tag",
    weightToken: "weight-regular",
  },
  {
    utility: "type-badge",
    className: "type-badge text-body-content",
    sample: "12",
    sizeToken: "font-size-tag",
    weightToken: "weight-bold",
  },
];

/** The spacing steps the style guide says are in use (``p-N`` / ``gap-N`` / ``m-N``, 4px a step). */
const SPACING_STEPS: readonly number[] = [0.5, 1, 1.5, 2, 3, 4, 5, 6, 8];

function valueIn(tokens: readonly DeclaredToken[], name: string): string {
  return tokens.find((token) => token.name === name)?.value ?? "?";
}

/** A theme token as a CSS value: the live variable, falling back to the declared value, since Tailwind emits a theme
 *  variable only where a utility reads it through ``var()``. */
function cssValueOf(token: DeclaredToken): string {
  return `var(--${token.name}, ${token.value})`;
}

/** A color's sample, drawn with the value the page wears: text as a word, a border as an outline, a fill as itself. */
function colorSample(color: ColorToken): m.Vnode {
  const paint = `var(--${color.variable})`;
  const sample = { "data-color-sample": "" };
  if (color.role === "text") {
    return m(
      "div",
      {
        ...sample,
        // Text meant for a colored surface ("on-accent") is shown on one.
        class: `flex h-6 w-12 items-center justify-center rounded-md type-label ${
          color.name.startsWith("on-") ? "bg-accent-surface" : ""
        }`,
      },
      m("span", { style: { color: paint } }, "Aa"),
    );
  }
  // A border color is drawn as a border; any other is a solid chip of it, with no outline.
  if (color.role === "border") {
    return m("div", { ...sample, class: "h-6 w-12 rounded-md border-2", style: { borderColor: paint } });
  }
  return m("div", { ...sample, class: "h-6 w-12 rounded-md", style: { backgroundColor: paint } });
}

/** One row of the color table: the sample, the utility and its value variable, and the value the page wears. */
function colorRow(color: ColorToken): m.Vnode {
  return m("tr", { id: `color-${color.name}`, class: "color-swatch border-t border-subtle" }, [
    m("td", { class: "py-1 pr-4 align-middle" }, colorSample(color)),
    m("td", { class: "py-1 pr-6 align-middle whitespace-nowrap" }, [
      m(
        "code",
        { class: "font-mono type-helper text-body-content select-all" },
        color.utility ?? `--${color.variable}`,
      ),
      color.utility === null
        ? null
        : m(
            "code",
            { class: "ml-2 font-mono type-helper text-body-content-tertiary select-all" },
            `--${color.variable}`,
          ),
    ]),
    m("td", { class: "w-full min-w-48 py-1 align-middle" }, inlineValue(color.variable, color.value)),
  ]);
}

/** A value as the page wears it on one line, with the standard value struck through after it when a theme changed it. */
function inlineValue(name: string, declared: string): m.Children {
  const themed = themeValue(name);
  const value = (text: string, extra: string): m.Vnode =>
    m("code", { class: `font-mono type-helper wrap-break-word ${extra}` }, text);
  return themed === null || themed === declared
    ? value(declared, "text-body-content-secondary")
    : [value(themed, "text-body-content-secondary"), " ", value(declared, "text-body-content-tertiary line-through")];
}

/** A token's value as the page wears it, and, when a theme has changed it, the standard value beside it. */
function valueCaptions(name: string, declared: string): m.Children {
  const themed = themeValue(name);
  return themed === null || themed === declared
    ? subCaption(declared)
    : [
        subCaption(themed),
        m("code", { class: "block font-mono type-helper text-body-content-tertiary line-through" }, declared),
      ];
}

/** What the page wears for a token: the theme's value when it sets one, else the standard one. */
function wornValue(name: string, declared: string): string {
  return themeValue(name) ?? declared;
}

/** The ramps, in the order the stylesheet keys them. */
const RAMPS: readonly string[] = ["neutral", "primary", "accent", "success", "warn", "danger", "wallpaper"];
const RAMP_STEPS: readonly number[] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];

/** A role's computed shade or a raw ramp step: the catalog shows those as ramps and in words, not as rows. */
function isShownAsRow(color: ColorToken): boolean {
  return !/-(hover|active|inactive)$/.test(color.name) && !/^c-[a-z]+-\d+$/.test(color.variable);
}

/** The colors to list as rows: the roles, without their computed shades or the raw ramp steps. */
function roleGroups(tokens: DesignTokens): DesignTokens["colorGroups"] {
  return tokens.colorGroups
    .filter((group) => !group.label.startsWith("ramp steps"))
    .map((group) => ({ ...group, colors: group.colors.filter(isShownAsRow) }))
    .filter((group) => group.colors.length > 0);
}

/** The workspace-color picker: Imbue Studio's colors, any other as a hex, and back to the workspace's own. */
function workspaceColorPicker(): m.Vnode {
  const current = previewedWorkspaceColor();
  const pick = (hex: string): void => void previewWorkspaceColor(hex).then(() => m.redraw());
  return m("div", { id: "workspace-color-preview", class: "mb-6 flex flex-wrap items-center gap-2" }, [
    m("span", { class: "mr-1 type-label text-body-content-secondary" }, "Preview a workspace color"),
    STUDIO_PALETTE.map(({ name, hex }) =>
      m("button", {
        type: "button",
        key: hex,
        "data-preview-hex": hex,
        "aria-pressed": current === hex ? "true" : "false",
        "aria-label": name,
        ...hoverTooltipAttrs(name),
        class:
          "h-6 w-6 cursor-pointer rounded-full border border-default " +
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus " +
          (current === hex ? "ring-2 ring-focus ring-offset-2" : ""),
        // The swatch is Imbue Studio's color itself, not a token: it is what is on offer.
        style: { backgroundColor: hex },
        onclick: () => pick(hex),
      }),
    ),
    m(
      "div",
      { class: "w-32" },
      m("input", {
        class: inputClass({ mono: true }),
        ...FIELD_PART,
        "aria-label": "Any color, as a hex",
        placeholder: "#rrggbb",
        onchange: (event: Event) => {
          const hex = canonicalHex((event.target as HTMLInputElement).value);
          if (hex !== null) pick(hex);
        },
      }),
    ),
    current === null
      ? null
      : m(
          Button,
          {
            variant: "ghost",
            sm: true,
            "data-preview-reset": "",
            onclick: () => {
              stopPreviewingWorkspaceColor();
              m.redraw();
            },
          },
          "Back to the workspace's own",
        ),
  ]);
}

/** Each ramp as a strip of its twelve steps, drawn with the values the page wears. */
function rampStrips(): m.Vnode {
  return m("div", { id: "color-ramps", class: "mb-8 grid gap-1.5" }, [
    m("div", { class: "grid grid-cols-[96px_repeat(12,minmax(0,1fr))] gap-1" }, [
      m("span"),
      RAMP_STEPS.map((step) =>
        m("span", { key: step, class: "text-center type-code-small text-body-content-tertiary" }, String(step)),
      ),
    ]),
    RAMPS.map((ramp) =>
      m(
        "div",
        { key: ramp, "data-ramp": ramp, class: "grid grid-cols-[96px_repeat(12,minmax(0,1fr))] items-center gap-1" },
        [
          m("span", { class: "type-label text-body-content capitalize" }, ramp),
          RAMP_STEPS.map((step) =>
            m("span", {
              key: step,
              "data-ramp-step": `${ramp}-${step}`,
              ...hoverTooltipAttrs(`--c-${ramp}-${step}`),
              class: "h-8 rounded-md",
              style: { backgroundColor: `var(--c-${ramp}-${step})` },
            }),
          ),
        ],
      ),
    ),
  ]);
}

function colorsSection(tokens: DesignTokens): CatalogSection {
  return {
    id: "colors",
    toc: "Colors",
    title: "Colors",
    desc: [
      "A color is one key per ramp, computed into twelve steps on one lightness ladder, so a step reads at the same contrast in every ramp. Views use the roles over the steps (",
      code("text-body-content"),
      ", ",
      code("bg-primary-surface"),
      ", ",
      code("border-default"),
      "); hand-written CSS uses the ",
      code("--c-*"),
      " value. Hover and active are computed from the surface they shade (",
      code("hover:bg-primary-surface-hover"),
      "), never tokens. The workspace color sets the keys: preview any color below, and every swatch, ramp and component on this page follows it.",
    ],
    body: () => [
      workspaceColorPicker(),
      rampStrips(),
      m("table", { class: "color-table w-full border-collapse text-left" }, [
        m(
          "thead",
          m("tr", [
            m("th", { class: "pb-1 pr-4 type-section text-body-content-tertiary font-normal" }, "Swatch"),
            m("th", { class: "pb-1 pr-6 type-section text-body-content-tertiary font-normal" }, "Name"),
            m("th", { class: "pb-1 type-section text-body-content-tertiary font-normal" }, "Value"),
          ]),
        ),
        roleGroups(tokens).map((group) =>
          m("tbody", { class: "color-group", "data-color-group": group.label }, [
            m("tr", m("th", { colspan: 3, class: "pt-4 pb-1 type-label text-body-content capitalize" }, group.label)),
            group.colors.map(colorRow),
          ]),
        ),
      ]),
    ],
  };
}

function typeSection(tokens: DesignTokens): CatalogSection {
  return {
    id: "type",
    toc: "Type",
    title: "Type roles",
    desc: [
      "The brand's type ramp, one utility per role, each bundling face, size, weight, line height and tracking. Besley sets the one headline a window exists to show; the sans sets anything you read, press, or that changes; the mono sets static labels and code. Color is separate: compose a role with a text color, e.g. ",
      code("type-title-sm text-body-content"),
      ". Menus, segmented controls and list rows are ",
      code("type-body-dense"),
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
              `${role.utility} -- ${wornValue(role.sizeToken, valueIn(tokens.scales, role.sizeToken))} / ` +
                `${wornValue(role.weightToken, valueIn(tokens.scales, role.weightToken))}` +
                (role.note === undefined ? "" : ` / ${role.note}`),
              "mt-0.5",
            ),
          ]),
        ),
      ),
      m("h3", { class: "type-section text-body-content-tertiary mt-8 mb-2" }, "Sizes and weights"),
      tokenTable(
        [...tokensWithPrefix(tokens.scales, "font-size-"), ...tokensWithPrefix(tokens.scales, "weight-")],
        (token) =>
          token.name.startsWith("font-size-")
            ? m("span", { style: { fontSize: `var(--${token.name})` } }, "The quick brown fox")
            : m("span", { style: { fontWeight: `var(--${token.name})` } }, "The quick brown fox"),
      ),
      m("h3", { class: "type-section text-body-content-tertiary mt-8 mb-2" }, "Font stacks"),
      m("div", { class: "flex flex-col gap-3" }, [
        m("div", { id: "font-sans" }, [
          m("p", { class: "font-sans type-body text-body-content" }, "font-sans -- The workspace's interface text"),
          valueCaptions("font-sans", valueIn(tokens.theme, "font-sans")),
        ]),
        m("div", { id: "font-mono" }, [
          m("p", { class: "font-mono type-body text-body-content" }, "font-mono -- ids, paths, code"),
          valueCaptions("font-mono", valueIn(tokens.theme, "font-mono")),
        ]),
        m("div", { id: "font-display" }, [
          m(
            "p",
            { class: "type-body text-body-content", style: { fontFamily: "var(--font-display)" } },
            "font-display -- headings, section labels, and a theme's titles, buttons and menus",
          ),
          valueCaptions("font-display", valueIn(tokens.theme, "font-display")),
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
      m("div", valueCaptions(token.name, token.value)),
      m("div", { id: `token-${token.name}`, class: "min-w-0 type-body text-body-content" }, demo(token)),
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
            class: "h-3 rounded-sm bg-accent-surface",
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
              class: "h-16 w-16 border border-default bg-body-surface-secondary",
              style: { borderRadius: cssValueOf(token) },
            }),
            caption(`rounded-${token.name.slice("radius-".length)}`),
            valueCaptions(token.name, token.value),
          ]),
        ),
        m("div", { id: "token-radius-full", class: "flex flex-col items-center gap-1.5" }, [
          m("div", { class: "h-16 w-16 rounded-full border border-default bg-body-surface-secondary" }),
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
        { class: "flex flex-wrap gap-8 rounded-lg bg-body-surface p-6" },
        // The values live in the value layer (--c-shadow-*), where a theme redeclares them.
        tokensWithPrefix(tokens.scales, "c-shadow-").map((token) =>
          m("div", { id: `token-${token.name}`, class: "flex max-w-64 flex-col gap-2" }, [
            m("div", {
              class: "h-20 w-40 rounded-lg border border-default bg-body-surface-raised",
              style: { boxShadow: `var(--${token.name})` },
            }),
            caption(token.name.slice("c-".length)),
            subCaption(`--${token.name}`),
            valueCaptions(token.name, token.value),
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
              { class: "relative h-6 w-64 rounded-full bg-body-surface-secondary" },
              m("div", {
                class:
                  "absolute top-1 left-1 h-4 w-4 rounded-full bg-accent-surface transition-transform ease-[ease] group-hover:translate-x-[232px]",
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
              class: "h-2 max-w-full rounded-sm bg-body-content-tertiary",
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
    ["alertDotIcon", alertDotIcon(14, "text-danger-surface")],
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
          m("div", { id: `icon-${name}`, class: "icon-tile flex items-center gap-2.5 text-body-content" }, [
            m.trust(icon(name, { size: 18 })),
            caption(name),
          ]),
        ),
      ),
      m("h3", { class: "type-section text-body-content-tertiary mt-6 mb-2" }, "Glyphs with their own builders"),
      m(
        "div",
        { class: "grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-x-4 gap-y-3" },
        special.map(([name, markup]) =>
          m("div", { id: `icon-${name}`, class: "icon-tile flex items-center gap-2.5 text-body-content" }, [
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
      colorsSection(tokens),
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
