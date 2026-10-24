/**
 * Where the workspace's Usage widget goes and how it looks there (app-widgets plan section 7.8): its place (the
 * desktop, the taskbar's row, called the toolbar, or the tray), its style and size on the desktop, its design in each
 * place in the taskbar, and how much it shows anywhere.
 * A choice the Usage app keeps in its own settings, not a flag. The Usage page picks it; the widget reads it with its
 * figures, and hears at once of a pick made in the same browser through a broadcast channel. ``?variant=``,
 * ``?size=``, ``?detail=``, ``?tray=`` and ``?toolbar=`` on the widget's URL override it for that page alone, so a
 * look can be tried without changing anyone's widget. Every pick also asks the shell to put this client's widget in
 * the picked place, where the pick is seen.
 */

import { fetchSettings, saveSettings } from "./api";
import type { UsageSettings } from "./api";

export const WIDGET_VARIANTS = ["rings", "minimal", "bars"] as const;
export type WidgetVariant = (typeof WIDGET_VARIANTS)[number];

export const WIDGET_SIZES = ["small", "medium", "large"] as const;
export type WidgetSize = (typeof WIDGET_SIZES)[number];

/** Less: the 5-hour limit alone and no spend. Standard: both limits and the spend. More: also each limit's figure and
 *  reset time in view. */
export const WIDGET_DETAILS = ["less", "standard", "more"] as const;
export type WidgetDetail = (typeof WIDGET_DETAILS)[number];

/** How the widget is drawn in the taskbar's tray: a ring pair with the name over the 5-hour figure; the ring pair
 *  alone; the name over two bars; the name and the 5-hour figure as text; the name beside the 5-hour figure over the
 *  weekly one. */
export const WIDGET_TRAYS = ["rings", "ring", "bars", "numbers", "stacked"] as const;
export type WidgetTray = (typeof WIDGET_TRAYS)[number];

/** Where the widget goes: the desktop; the taskbar's row, among the windows' entries; or its tray at the right end.
 *  The shell's placement names, so a pick is passed to it as it is. */
export const WIDGET_PLACES = ["desktop", "toolbar", "tray"] as const;
export type WidgetPlace = (typeof WIDGET_PLACES)[number];

/** How the widget is drawn in the bar's row: a chip like a window's entry; upright meters; one line of text; the
 *  name and figure over thin lines. */
export const WIDGET_TOOLBARS = ["chip", "meters", "inline", "line"] as const;
export type WidgetToolbar = (typeof WIDGET_TOOLBARS)[number];

export interface WidgetLook {
  readonly variant: WidgetVariant;
  readonly size: WidgetSize;
  readonly detail: WidgetDetail;
  readonly tray: WidgetTray;
  readonly place: WidgetPlace;
  readonly toolbar: WidgetToolbar;
}

export const DEFAULT_WIDGET_LOOK: WidgetLook = {
  variant: "rings",
  size: "medium",
  detail: "standard",
  tray: "rings",
  place: "desktop",
  toolbar: "chip",
};

/** What each choice is called where it is picked. */
export const WIDGET_VARIANT_LABELS: Record<WidgetVariant, string> = {
  rings: "Rings",
  minimal: "Rings · Minimal",
  bars: "Bars",
};
export const WIDGET_SIZE_LABELS: Record<WidgetSize, string> = { small: "Small", medium: "Medium", large: "Large" };
export const WIDGET_DETAIL_LABELS: Record<WidgetDetail, string> = { less: "Less", standard: "Standard", more: "More" };
export const WIDGET_TRAY_LABELS: Record<WidgetTray, string> = {
  rings: "Rings",
  ring: "Ring only",
  bars: "Bars",
  numbers: "Numbers",
  stacked: "Stacked",
};
export const WIDGET_PLACE_LABELS: Record<WidgetPlace, string> = {
  desktop: "Desktop",
  toolbar: "Toolbar",
  tray: "Tray",
};
export const WIDGET_TOOLBAR_LABELS: Record<WidgetToolbar, string> = {
  chip: "Chip",
  meters: "Meters",
  inline: "Inline",
  line: "Line",
};

/** The channel a pick is announced on, to every page of this app in the same browser. */
export const WIDGET_LOOK_CHANNEL = "usage-widget-look";

function oneOf<T extends string>(choices: readonly T[], value: unknown): T | null {
  return choices.find((choice) => choice === value) ?? null;
}

/** ``value`` as a whole look, or null unless every part of it is a known choice. */
export function asWidgetLook(value: unknown): WidgetLook | null {
  if (value === null || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const variant = oneOf(WIDGET_VARIANTS, record.variant);
  const size = oneOf(WIDGET_SIZES, record.size);
  const detail = oneOf(WIDGET_DETAILS, record.detail);
  const tray = oneOf(WIDGET_TRAYS, record.tray);
  const place = oneOf(WIDGET_PLACES, record.place);
  const toolbar = oneOf(WIDGET_TOOLBARS, record.toolbar);
  return variant === null || size === null || detail === null || tray === null || place === null || toolbar === null
    ? null
    : { variant, size, detail, tray, place, toolbar };
}

/** The settings as a look: a part the settings do not name, or name as an unknown choice, takes its default. */
export function lookOfSettings(settings: UsageSettings): WidgetLook {
  return {
    variant: oneOf(WIDGET_VARIANTS, settings.widget_variant) ?? DEFAULT_WIDGET_LOOK.variant,
    size: oneOf(WIDGET_SIZES, settings.widget_size) ?? DEFAULT_WIDGET_LOOK.size,
    detail: oneOf(WIDGET_DETAILS, settings.widget_detail) ?? DEFAULT_WIDGET_LOOK.detail,
    tray: oneOf(WIDGET_TRAYS, settings.widget_tray) ?? DEFAULT_WIDGET_LOOK.tray,
    place: oneOf(WIDGET_PLACES, settings.widget_place) ?? DEFAULT_WIDGET_LOOK.place,
    toolbar: oneOf(WIDGET_TOOLBARS, settings.widget_toolbar) ?? DEFAULT_WIDGET_LOOK.toolbar,
  };
}

function settingsOfLook(look: WidgetLook): UsageSettings {
  return {
    widget_variant: look.variant,
    widget_size: look.size,
    widget_detail: look.detail,
    widget_tray: look.tray,
    widget_place: look.place,
    widget_toolbar: look.toolbar,
  };
}

/** The parts of the look a widget URL's query names, each only when it is a known choice. */
export function lookOverrides(search: string): Partial<WidgetLook> {
  const query = new URLSearchParams(search);
  const overrides: {
    variant?: WidgetVariant;
    size?: WidgetSize;
    detail?: WidgetDetail;
    tray?: WidgetTray;
    toolbar?: WidgetToolbar;
  } = {};
  const variant = oneOf(WIDGET_VARIANTS, query.get("variant"));
  const size = oneOf(WIDGET_SIZES, query.get("size"));
  const detail = oneOf(WIDGET_DETAILS, query.get("detail"));
  if (variant !== null) overrides.variant = variant;
  if (size !== null) overrides.size = size;
  if (detail !== null) overrides.detail = detail;
  const tray = oneOf(WIDGET_TRAYS, query.get("tray"));
  if (tray !== null) overrides.tray = tray;
  const toolbar = oneOf(WIDGET_TOOLBARS, query.get("toolbar"));
  if (toolbar !== null) overrides.toolbar = toolbar;
  return overrides;
}

/** The workspace's look as stored, or null when the settings cannot be read, so a reader keeps what it had rather
 *  than flip to the default on a failed read. */
export async function readWidgetLook(): Promise<WidgetLook | null> {
  try {
    return lookOfSettings(await fetchSettings());
  } catch (error) {
    console.warn("[usage] could not read the widget's settings:", error);
    return null;
  }
}

export interface ChannelLike {
  postMessage(message: unknown): void;
  onmessage: ((event: MessageEvent) => void) | null;
  close(): void;
}

export interface WidgetLookPickerDeps {
  redraw: () => void;
  openChannel: () => ChannelLike;
  /** Ask the shell to put this client's Usage widget at ``place``. */
  placeWidget: (place: WidgetPlace) => void;
}

/** The Usage page's picker: the stored look, and a change to one part of it saved and announced to the widget. */
export class WidgetLookPicker {
  look: WidgetLook | null = null;
  errorMessage: string | null = null;

  constructor(private readonly deps: WidgetLookPickerDeps) {}

  /** Read the stored look. One that cannot be read leaves ``look`` null, so nothing is offered to pick: a pick saves
   *  the whole look, and saving the defaults for the parts not picked would overwrite the workspace's choices. */
  async load(): Promise<void> {
    const look = await readWidgetLook();
    this.look = look;
    this.errorMessage = look === null ? "Could not read the widget's settings" : null;
    this.deps.redraw();
  }

  async select(change: Partial<WidgetLook>): Promise<void> {
    const previous = this.look ?? DEFAULT_WIDGET_LOOK;
    const next = { ...previous, ...change };
    this.look = next;
    this.errorMessage = null;
    this.deps.redraw();
    try {
      await saveSettings(settingsOfLook(next));
    } catch (error) {
      this.look = previous;
      this.errorMessage = `Could not change the widget: ${error instanceof Error ? error.message : String(error)}`;
      this.deps.redraw();
      return;
    }
    const channel = this.deps.openChannel();
    channel.postMessage(next);
    channel.close();
    // The page shows only the picked place's choices, so whatever was picked is seen there: this client's widget goes
    // there too.
    this.deps.placeWidget(next.place);
  }
}
