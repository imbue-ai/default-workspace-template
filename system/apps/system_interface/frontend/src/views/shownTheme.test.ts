import { afterEach, describe, expect, it, vi } from "vitest";
import type { ThemeRef } from "@imbue/workspace-ui/src/themes/themeClient";
import type { ShownTheme } from "../store/DesktopStore";
import { themeRecord } from "../testing/records";
import { type ShownThemeActions, followShownTheme } from "./shownTheme";

afterEach(() => {
  vi.restoreAllMocks();
});

interface Wearing {
  readonly theme: ThemeRef;
  readonly isRemembered: boolean;
  /** The page shows the theme: the wear settles. */
  readonly show: () => void;
}

/** Follow a store that hands its listener to the test, with actions that record what they are asked, in order. */
function follow(takeMetrics: () => void = () => undefined): {
  notify: (shown: ShownTheme) => void;
  worn: Wearing[];
  events: string[];
} {
  let listener: ((shown: ShownTheme) => void) | null = null;
  const worn: Wearing[] = [];
  const events: string[] = [];
  const actions: ShownThemeActions = {
    wear: (theme, isRemembered) =>
      new Promise((resolve) => {
        events.push(`wear ${theme.id}`);
        worn.push({ theme, isRemembered, show: resolve });
      }),
    afterLoad: () => {
      events.push("loaded");
      return Promise.resolve();
    },
    takeMetrics: () => {
      events.push("metrics");
      takeMetrics();
    },
    setIconTheme: (theme) => events.push(`icons ${theme.id}`),
  };
  followShownTheme(
    {
      onShownThemeChanged: (heard) => {
        listener = heard;
        return () => undefined;
      },
    },
    actions,
  );
  const notify = (shown: ShownTheme): void => {
    if (listener === null) throw new Error("followShownTheme never listened");
    listener(shown);
  };
  return { notify, worn, events };
}

describe("followShownTheme", () => {
  it("wears a preview without remembering it, and takes the metrics only once the theme shows", async () => {
    const { notify, worn, events } = follow();

    notify({ record: themeRecord("mac-classic", { revision: "r1" }), isPreview: true });
    await Promise.resolve();

    expect(worn.map(({ theme, isRemembered }) => [theme, isRemembered])).toEqual([
      [{ id: "mac-classic", revision: "r1" }, false],
    ]);
    expect(events).toEqual(["icons mac-classic", "wear mac-classic"]);

    worn[0].show();
    await vi.waitFor(() => expect(events).toEqual(["icons mac-classic", "wear mac-classic", "loaded", "metrics"]));
  });

  it("takes the metrics again when the same theme is shown again, since its stylesheet may have failed before", async () => {
    const { notify, worn, events } = follow();
    const record = themeRecord("windows-2000", { revision: "r1" });

    notify({ record, isPreview: false });
    worn[0].show();
    await vi.waitFor(() => expect(events.filter((event) => event === "metrics")).toHaveLength(1));
    notify({ record: { ...record }, isPreview: false });
    worn[1].show();

    await vi.waitFor(() => expect(events.filter((event) => event === "metrics")).toHaveLength(2));
    expect(worn[1].isRemembered).toBe(true);
  });

  it("logs a failure to take the metrics rather than leaving it unhandled", async () => {
    const logged = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const failure = new Error("the theme token --desk-title-bar-height is not a pixel length");
    const { notify, worn } = follow(() => {
      throw failure;
    });

    notify({ record: themeRecord("paper"), isPreview: false });
    worn[0].show();

    await vi.waitFor(() =>
      expect(logged).toHaveBeenCalledWith("[theme] could not take the metrics of the theme paper", failure),
    );
  });
});
