// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";

import { listingOf, rowOf, runOf } from "../testing/records";
import { ADD_PREFIX, ListPage, groupRows } from "./ListPage";

afterEach(() => unmountViews());

describe("groupRows", () => {
  it("groups by the app's display name, with the workspace's own last", () => {
    const rows = [
      rowOf({ routine_id: "a", app: null }),
      rowOf({ routine_id: "b", app: "news" }),
      rowOf({ routine_id: "c", app: "other" }),
    ];
    const groups = groupRows(rows, { news: "Daily News" });
    expect(groups.map(([heading, members]) => [heading, members.map((r) => r.routine_id)])).toEqual([
      ["Daily News", ["b"]],
      ["other", ["c"]],
      ["Workspace", ["a"]],
    ]);
  });

  it("keeps an app called Workspace apart from the workspace's own rows", () => {
    const rows = [rowOf({ routine_id: "a", app: null }), rowOf({ routine_id: "b", app: "ws" })];
    const groups = groupRows(rows, { ws: "Workspace" });
    expect(groups.map(([heading, members]) => [heading, members.map((r) => r.routine_id)])).toEqual([
      ["Workspace", ["b"]],
      ["Workspace", ["a"]],
    ]);
  });
});

describe("ListPage", () => {
  it("draws one linked row per routine with its health, schedule and last run", () => {
    const rows = [
      rowOf({
        routine_id: "digest",
        title: "Inbox digest",
        health: "FAILING",
        health_text: "Failing",
        health_detail: "Last worked 2 hours ago; 3 failed tries in a row.",
        last_run: runOf({ outcome: "FAILED", outcome_text: "Failed", summary: "No mail server" }),
        last_run_text: "Failed 10 minutes ago, automatic",
      }),
      rowOf({}),
    ];
    const root = mountView(() => m(ListPage, { listing: listingOf(rows), onStartWithText: () => undefined }));

    const links = [...root.querySelectorAll<HTMLAnchorElement>("a.routine-row")];
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/digest", "/news-sync"]);
    expect(links[0].querySelector("[data-health]")?.getAttribute("data-health")).toBe("FAILING");
    // The health's detail, when it says more than the pill, and the last run and its summary beside it.
    expect(links[0].textContent).toContain("Last worked 2 hours ago; 3 failed tries in a row.");
    expect(links[0].textContent).toContain("Failed 10 minutes ago, automatic");
    expect(links[0].textContent).toContain("No mail server");
    expect(links[1].textContent).toContain("Every hour");
    expect(links[1].textContent).toContain("Worked 10 minutes ago, automatic");
    const stats = [...root.querySelectorAll("[data-summary] > div")].map((stat) => stat.textContent);
    expect(stats).toEqual(["2Routines", "1Need attention", "0Running", "today 8:00 amNext run"]);
  });

  it("shows a running routine as running", () => {
    const root = mountView(() =>
      m(ListPage, { listing: listingOf([rowOf({ is_running: true })]), onStartWithText: () => undefined }),
    );
    expect(root.querySelector("[data-health]")?.getAttribute("data-health")).toBe("RUNNING");
  });

  it("groups the rows under the apps' display names from the listing", () => {
    const rows = [rowOf({ routine_id: "a", app: null }), rowOf({ routine_id: "b", app: "news" })];
    const root = mountView(() =>
      m(ListPage, { listing: listingOf(rows, { news: "Daily News" }), onStartWithText: () => undefined }),
    );
    expect([...root.querySelectorAll("[data-group]")].map((group) => group.getAttribute("data-group"))).toEqual([
      "Daily News",
      "Workspace",
    ]);
  });

  it("hands the add field's text to the shell with the add prefix, keeping it across a redraw", () => {
    const started: string[] = [];
    const root = mountView(() =>
      m(ListPage, { listing: listingOf([]), onStartWithText: (text) => started.push(text) }),
    );
    const input = root.querySelector<HTMLInputElement>("[data-add-input]")!;
    input.value = "every weekday at 8, summarize my inbox";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    // A poll redraws the page between the typing and the submit.
    m.redraw.sync();
    root
      .querySelector<HTMLFormElement>("[data-add-form]")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    expect(started).toEqual([`${ADD_PREFIX}every weekday at 8, summarize my inbox`]);
    m.redraw.sync();
    expect(root.querySelector<HTMLInputElement>("[data-add-input]")!.value).toBe("");
    expect(root.querySelector("[data-empty]")).not.toBeNull();
  });
});
