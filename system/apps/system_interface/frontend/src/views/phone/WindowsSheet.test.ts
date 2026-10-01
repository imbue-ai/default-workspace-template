// @vitest-environment jsdom
/**
 * The phone's windows sheet: its search field narrows the rows past six windows, and once the field is gone (a
 * close brought the count down) every window is listed again.
 */
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
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

function listedIds(sheetRows: readonly PhoneWindowRow[], query: string): string[] {
  const root = mountView(() =>
    m(WindowsSheet, {
      rows: sheetRows,
      shownWindowId: null,
      avatar: INITIAL_AVATAR_STATE,
      query,
      onQuery: () => undefined,
      onShow: () => undefined,
      onClose: () => undefined,
      onMenu: () => undefined,
      onCloseAll: () => undefined,
      onDismiss: () => undefined,
    }),
  );
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
