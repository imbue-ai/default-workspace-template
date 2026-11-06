// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";

import { runOf } from "../testing/records";
import { LogPage } from "./LogPage";

afterEach(() => unmountViews());

describe("LogPage", () => {
  it("shows the whole log as text under links back to the list, the routine and the run", () => {
    const root = mountView(() =>
      m(LogPage, { run: runOf(), log: "<b>not markup</b>\nline 2\n", routineTitle: "News sync" }),
    );
    expect([...root.querySelectorAll("a")].map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
      ["All routines", "/"],
      ["News sync", "/news-sync"],
      ["Worked, 10 minutes ago", "/news-sync/runs/run-1"],
    ]);
    const log = root.querySelector("[data-log]")!;
    expect(log.textContent).toBe("<b>not markup</b>\nline 2\n");
    expect(log.querySelector("b")).toBeNull();
  });

  it("says when nothing was printed", () => {
    const root = mountView(() => m(LogPage, { run: runOf(), log: "", routineTitle: "News sync" }));
    expect(root.querySelector("[data-log]")?.textContent).toBe("Nothing was printed.");
  });
});
