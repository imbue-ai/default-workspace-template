import { afterEach, describe, expect, it, vi } from "vitest";

import { WidgetLookPicker, asWidgetLook, lookOfSettings, lookOverrides, readWidgetLook } from "./widgetLook";
import type { ChannelLike } from "./widgetLook";

/** A Usage server that answers its settings with ``stored`` and records what is posted; ``status`` for every request
 *  when it is set. */
function stubServer(stored: unknown, status: number | null = null): unknown[] {
  const posted: unknown[] = [];
  vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
    if (status !== null) return new Response(JSON.stringify({ detail: "refused" }), { status });
    if (init?.method === "POST") {
      const body: unknown = JSON.parse(String(init.body));
      posted.push(body);
      return new Response(JSON.stringify(body), { status: 200 });
    }
    return new Response(JSON.stringify(stored), { status: 200 });
  });
  return posted;
}

function recordingChannel(): { channel: ChannelLike; sent: unknown[] } {
  const sent: unknown[] = [];
  return { channel: { postMessage: (message) => sent.push(message), onmessage: null, close: () => undefined }, sent };
}

afterEach(() => vi.unstubAllGlobals());

const STORED = {
  widget_variant: "rings",
  widget_size: "medium",
  widget_detail: "standard",
  widget_tray: "rings",
  widget_place: "desktop",
  widget_toolbar: "chip",
};
const LOOK = {
  variant: "rings",
  size: "medium",
  detail: "standard",
  tray: "rings",
  place: "desktop",
  toolbar: "chip",
};

describe("the widget's look", () => {
  it("reads known choices from a query, a message, or the settings, each unknown one taking its default", () => {
    expect(lookOverrides("?variant=bars&size=huge&detail=more&tray=stacked&toolbar=meters")).toEqual({
      variant: "bars",
      detail: "more",
      tray: "stacked",
      toolbar: "meters",
    });
    expect(lookOverrides("?toolbar=sparkle")).toEqual({});
    expect(asWidgetLook({ ...LOOK, size: "small", place: "toolbar", toolbar: "inline" })).toEqual({
      ...LOOK,
      size: "small",
      place: "toolbar",
      toolbar: "inline",
    });
    expect(asWidgetLook({ ...LOOK, place: "dock" })).toBeNull();
    expect(asWidgetLook({ variant: "rings", size: "small" })).toBeNull();
    // An older file, from before the place was picked, reads whole with the defaults for the parts it lacks.
    expect(
      lookOfSettings({
        widget_variant: "bars",
        widget_size: "huge",
        widget_detail: "less",
        widget_tray: "sparkle",
      } as unknown as typeof STORED),
    ).toEqual({ ...LOOK, variant: "bars", detail: "less" });
  });

  it("reads the stored look, and nothing when the settings cannot be read", async () => {
    stubServer({ ...STORED, widget_variant: "bars", widget_place: "toolbar", widget_toolbar: "line" });
    expect(await readWidgetLook()).toEqual({ ...LOOK, variant: "bars", place: "toolbar", toolbar: "line" });
    stubServer({}, 503);
    expect(await readWidgetLook()).toBeNull();
  });

  it("saves a change to one part and announces the whole look, and puts the old one back when refused", async () => {
    const { channel, sent } = recordingChannel();
    const placeWidget = vi.fn();
    const picker = new WidgetLookPicker({ redraw: () => undefined, openChannel: () => channel, placeWidget });
    const posted = stubServer(STORED);
    await picker.load();
    expect(picker.look).toEqual(LOOK);

    await picker.select({ size: "large" });
    expect(picker.look).toEqual({ ...LOOK, size: "large" });
    expect(posted).toEqual([{ ...STORED, widget_size: "large" }]);
    expect(sent).toEqual([{ ...LOOK, size: "large" }]);

    stubServer({}, 500);
    await picker.select({ variant: "bars" });
    expect(picker.look?.variant).toBe("rings");
    expect(picker.errorMessage).toContain("Could not change the widget");
    expect(sent).toHaveLength(1);
    // A refused pick moves nothing.
    expect(placeWidget.mock.calls).toEqual([["desktop"]]);
  });

  it("offers nothing to pick when the stored look cannot be read, so no pick overwrites it", async () => {
    const { channel } = recordingChannel();
    const picker = new WidgetLookPicker({ redraw: () => undefined, openChannel: () => channel, placeWidget: vi.fn() });
    stubServer({}, 503);
    await picker.load();
    expect(picker.look).toBeNull();
    expect(picker.errorMessage).toContain("Could not read");
    stubServer(STORED);
    await picker.load();
    expect(picker.look).toEqual(LOOK);
    expect(picker.errorMessage).toBeNull();
  });

  it("puts this client's widget in the picked place on every pick, where the pick is seen", async () => {
    const { channel } = recordingChannel();
    const placeWidget = vi.fn();
    const picker = new WidgetLookPicker({ redraw: () => undefined, openChannel: () => channel, placeWidget });
    stubServer(STORED);
    await picker.load();
    expect(placeWidget).not.toHaveBeenCalled();

    await picker.select({ place: "tray" });
    await picker.select({ tray: "stacked" });
    await picker.select({ place: "toolbar" });
    await picker.select({ toolbar: "meters" });
    await picker.select({ place: "desktop" });
    expect(picker.look).toEqual({ ...LOOK, tray: "stacked", toolbar: "meters" });
    expect(placeWidget.mock.calls).toEqual([["tray"], ["tray"], ["toolbar"], ["toolbar"], ["desktop"]]);
  });
});
