/**
 * "Start something": the ways INTO the product the New Tab page offers as tiles, each a title, a
 * sentence, a glyph, and the first message of the chat it starts. Where "Open new" spins up an
 * empty object, these name an intent; the prompts are plain-language requests written so the
 * mind's own skills match on them, and name no skill.
 *
 * One tile is the exception: "Start from a template" has its answer further down the same page,
 * so it scrolls there instead of opening anything (``prompt`` is null).
 *
 * Each tile has a hue from the Minds brand palette (the sheet the workspace accents come from).
 * The tile itself stays a white card; the hue shows in the glyph, drawn duotone off it: the hue
 * darkened for the stroke, a wash of it for the fill of whatever shapes the glyph closes.
 *
 * The grid shows START_PAGE_SIZE tiles at a time; "See more" reveals the next page and stays
 * until every tile is shown. The paging arithmetic and the search match are pure and tested.
 */

import { matchesQuery } from "../models/search";

export interface StartOption {
  /** Stable marker (``data-start``) and vnode key. */
  key: string;
  title: string;
  description: string;
  /** Inner SVG markup on a 24x24 grid, in the shared stroke-outline style. */
  glyphPaths: string;
  /** The tile's brand-palette colour, as a hex triplet; the glyph's two tones are mixed from it. */
  hue: string;
  /** The seeded first message of the chat this tile starts; null scrolls to the templates. */
  prompt: string | null;
}

export const START_PAGE_SIZE = 6;

export const START_OPTIONS: readonly StartOption[] = [
  {
    key: "build-app",
    hue: "#cecd0c", // energy
    title: "Build a new app",
    description: "Work through it together, one stage at a time, with the choices laid out for you.",
    glyphPaths:
      '<rect width="7" height="7" x="14" y="3" rx="1"/>' +
      '<path d="M10 21V8a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5a1 1 0 0 0-1-1H3"/>',
    prompt:
      "I want to build a new app. Help me work out what it should do, show me a quick mock of the look " +
      "and feel first, and then build it for me one stage at a time.",
  },
  {
    key: "template",
    hue: "#8eafcb", // peace
    title: "Start from a template",
    description: "Adopt something another person already built and make it yours.",
    glyphPaths:
      '<rect width="18" height="7" x="3" y="3" rx="1"/><rect width="9" height="7" x="3" y="14" rx="1"/>' +
      '<rect width="5" height="7" x="16" y="14" rx="1"/>',
    prompt: null,
  },
  {
    key: "connect-data",
    hue: "#492222", // courage
    title: "Connect your data",
    description: "Link an account you already use, so your apps and chats can work from what is in it.",
    glyphPaths:
      '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/>' +
      '<path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
    prompt:
      "Help me connect an account I already use, like Gmail, Slack, Notion, or GitHub, so my apps and " +
      "chats can work with what is in it. Ask me which one, then walk me through connecting it.",
  },
  {
    key: "routine",
    hue: "#d26645", // respect
    title: "Set up a routine",
    description: "Something that runs on its own schedule, like a briefing every morning.",
    glyphPaths: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    prompt:
      "Help me set up a routine that runs on its own schedule, like a briefing every morning. Ask me " +
      "what it should do and when it should run, then set it up.",
  },
  {
    key: "delegate",
    hue: "#4b4c08", // envy
    title: "Delegate a task",
    description: "Hand something over and walk away. It comes back when the work is done.",
    glyphPaths: '<path d="M21 10.5V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12.5"/><path d="m9 11 3 3L22 4"/>',
    prompt:
      "I have a task I would like to hand off entirely. Ask me what it is and anything you need to know, " +
      "then go do it and report back when it is done.",
  },
  {
    key: "make-sense",
    hue: "#e4999a", // belonging
    title: "Make sense of a pile of stuff",
    description: "Point at files, an export or an inbox and get something you can actually read.",
    glyphPaths:
      '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/>' +
      '<path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    prompt:
      "I have a pile of files, an export, or an inbox I need to make sense of. Ask me where it is, then " +
      "turn it into something I can actually read.",
  },
  {
    key: "learn",
    hue: "#f5d6a0", // comfort
    title: "Learn about Minds",
    description: "Have Minds teach you about all of its different capabilities and features.",
    glyphPaths:
      '<path d="M21.42 10.922a1 1 0 0 0-.019-1.838L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08a1 1 0 0 0 0 1.832l8.57 3.908a2 2 0 0 0 1.66 0z"/>' +
      '<path d="M22 10v6"/><path d="M6 12.5V16a6 3 0 0 0 12 0v-3.5"/>',
    prompt:
      "Teach me about Minds: walk me through its different capabilities and features, and show me what " +
      "I can do from here.",
  },
  {
    key: "edit-minds",
    hue: "#0b292b", // confusion
    title: "Edit Minds itself",
    description: "Change the interface, theme, chats, etc--Minds can modify itself!",
    glyphPaths:
      '<path d="m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.2 1.2 0 0 0 1.72 0L21.64 5.36a1.2 1.2 0 0 0 0-1.72"/>' +
      '<path d="m14 7 3 3"/><path d="M5 6v4"/><path d="M19 14v4"/><path d="M10 2v2"/><path d="M7 8H3"/>' +
      '<path d="M21 16h-4"/><path d="M11 3H9"/>',
    prompt:
      "I want to change Minds itself: the interface, the theme, how chats look or behave, or anything " +
      "else about it. Show me what can be changed and help me make the change.",
  },
];

/** The tiles shown so far: the first ``shownCount`` of them. */
export function visibleStartOptions(options: readonly StartOption[], shownCount: number): StartOption[] {
  return options.slice(0, Math.max(0, shownCount));
}

/** How many tiles "See more" shows next: one more page, never past the end. */
export function nextStartCount(shownCount: number, totalCount: number): number {
  return Math.min(shownCount + START_PAGE_SIZE, totalCount);
}

/** Whether "See more" still has anything to reveal. */
export function hasMoreStartOptions(shownCount: number, totalCount: number): boolean {
  return shownCount < totalCount;
}

/** The tiles a query finds: by title or description. */
export function searchStartOptions(options: readonly StartOption[], query: string): StartOption[] {
  return options.filter((option) => matchesQuery(query, option.title, option.description));
}

const XMLNS = "http://www.w3.org/2000/svg";

/**
 * The glyph's two tones on a white tile, mixed from the tile's hue: the hue darkened enough to read
 * against the page for the stroke, and a wash of it for the fill. The duotone comes from the
 * glyph's own geometry: its closed shapes take the fill, its open lines enclose nothing and stay
 * pure stroke. Both tones are color-mix() values, written straight into the svg's fill and stroke.
 */
export function glyphTones(option: StartOption): { stroke: string; fill: string } {
  return {
    stroke: `color-mix(in srgb, ${option.hue}, black 30%)`,
    fill: `color-mix(in srgb, ${option.hue}, white 75%)`,
  };
}

/** A tile's glyph, in the shared stroke-outline frame: tinted in the tile's hue, or inheriting the
 *  text colour (the standing-down look) when ``isTinted`` is false. */
export function startGlyph(option: StartOption, size: number, isTinted: boolean): string {
  const tones = glyphTones(option);
  const colours = isTinted ? `fill="${tones.fill}" stroke="${tones.stroke}"` : 'fill="none" stroke="currentColor"';
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" ${colours} ` +
    `stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${option.glyphPaths}</svg>`
  );
}
