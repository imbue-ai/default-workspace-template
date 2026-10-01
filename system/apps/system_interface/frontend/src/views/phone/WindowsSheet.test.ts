// @vitest-environment jsdom
/**
 * The phone's windows sheet: its search field narrows the rows past six windows, and once the field is gone (a
 * close brought the count down) every window is listed again; a row is shown from the keyboard as from a tap.
 */
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { INITIAL_AVATAR_STATE } from "../../reducers/desktopState";
import type { PhoneWindowRow } from "../../reducers/phone";
import { appRecord, windowRecord } from "../../testing/records";
import { WINDOW_SEARCH_THRESHOLD, WindowsSheet } from "./WindowsSheet";

afterEach(unmountViews);

const docs = appRecord("docs");
const notes = appRecord("notes");

function rows(count: number): PhoneWindowRow[] {
  return Array.from({ length: count }, (_, index) => {
    const app = index === 0 ? docs : notes;
    return { window: windowRecord(`win-${index}`, app.name, "/"), app, title: `${app.display_name} ${index}` };
  });
}

function mountSheet(sheetRows: readonly PhoneWindowRow[], query: string, onShow: (windowId: string) => void): Element {
  return mountView(() =>
    m(WindowsSheet, {
      rows: sheetRows,
      shownWindowId: null,
      avatar: INITIAL_AVATAR_STATE,
      query,
      onQuery: () => undefined,
      onShow,
      onClose: () => undefined,
      onMenu: () => undefined,
      onCloseAll: () => undefined,
      onDismiss: () => undefined,
    }),
  );
}

function listedIds(sheetRows: readonly PhoneWindowRow[], query: string): string[] {
  const root = mountSheet(sheetRows, query, () => undefined);
  return [...root.querySelectorAll<HTMLElement>("[data-phone-window-row]")].map(
    (row) => row.dataset.phoneWindowRow ?? "",
  );
}

describe("the windows sheet's search", () => {
  it("narrows the rows while the field is offered, and lists every window once it is not", () => {
    expect(listedIds(rows(WINDOW_SEARCH_THRESHOLD + 1), "docs")).toEqual(["win-0"]);
    expect(document.querySelector("[data-phone-window-search]")).not.toBeNull();
    unmountViews();

    expect(listedIds(rows(WINDOW_SEARCH_THRESHOLD), "docs")).toHaveLength(WINDOW_SEARCH_THRESHOLD);
    expect(document.querySelector("[data-phone-window-search]")).toBeNull();
  });
});

describe("a windows-sheet row from the keyboard", () => {
  it("shows its window on Enter or Space, and leaves the keys of its kebab to the kebab", () => {
    const onShow = vi.fn<(windowId: string) => void>();
    const root = mountSheet(rows(2), "", onShow);
    const row = root.querySelector<HTMLElement>('[data-phone-window-row="win-1"]');
    const kebab = root.querySelector<HTMLElement>('[data-phone-window-menu="win-1"]');
    if (row === null || kebab === null) throw new Error("no row");

    for (const key of ["Enter", " "]) row.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    kebab.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    expect(onShow.mock.calls).toEqual([["win-1"], ["win-1"]]);
  });
});
