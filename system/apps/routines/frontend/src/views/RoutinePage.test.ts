// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import { detailOf, rowOf, runOf } from "../testing/records";
import { RoutinePage, type RoutinePageAttrs } from "./RoutinePage";

afterEach(() => unmountViews());

function mount(attrs: Partial<RoutinePageAttrs>): HTMLElement {
  return mountView(() =>
    m(RoutinePage, {
      detail: detailOf(),
      isBusy: false,
      onRunNow: () => undefined,
      onToggleEnabled: () => undefined,
      onChangeSchedule: () => undefined,
      onFix: () => undefined,
      onRemove: () => undefined,
      ...attrs,
    }),
  );
}

function isDisabled(root: HTMLElement, selector: string): boolean {
  return root.querySelector<HTMLButtonElement>(selector)!.disabled;
}

describe("RoutinePage", () => {
  it("shows the definition in words and offers every control to a working routine", () => {
    const onRunNow = vi.fn();
    const root = mount({ onRunNow });
    expect(root.querySelector("h1")?.textContent).toBe("News sync");
    expect(root.textContent).toContain("Every hour (next: today 8:00 am)");
    expect(root.textContent).toContain("python3 sync.py");
    expect(root.textContent).toContain("system/routines/news-sync.toml");
    expect(root.querySelector("[data-fix]")).toBeNull();
    expect(root.querySelector("[data-toggle]")?.textContent).toBe("Turn off");
    for (const control of ["[data-run-now]", "[data-toggle]", "[data-change-schedule]", "[data-remove]"]) {
      expect(isDisabled(root, control), control).toBe(false);
    }
    root.querySelector<HTMLButtonElement>("[data-run-now]")!.click();
    expect(onRunNow).toHaveBeenCalledTimes(1);
  });

  it("offers Fix it to a failing routine and names its failure", () => {
    const onFix = vi.fn();
    const root = mount({
      detail: detailOf({
        row: rowOf({ health: "FAILING", health_text: "Failing", health_detail: "Never worked yet; 3 failed tries." }),
      }),
      onFix,
    });
    expect(root.querySelector("[data-health-detail]")?.textContent).toBe("Never worked yet; 3 failed tries.");
    root.querySelector<HTMLButtonElement>("[data-fix]")!.click();
    expect(onFix).toHaveBeenCalledTimes(1);
  });

  it("keeps a broken routine's problem on screen and lets it only be fixed or removed", () => {
    const root = mount({
      detail: detailOf({
        row: rowOf({ health: "BROKEN", health_text: "Broken", error: "every: 'every' is at least a minute" }),
        definition: null,
      }),
    });
    expect(root.textContent).toContain("every: 'every' is at least a minute");
    expect(isDisabled(root, "[data-run-now]")).toBe(true);
    expect(isDisabled(root, "[data-toggle]")).toBe(true);
    expect(isDisabled(root, "[data-change-schedule]")).toBe(true);
    expect(isDisabled(root, "[data-fix]")).toBe(false);
    expect(isDisabled(root, "[data-remove]")).toBe(false);
  });

  it("shows a running routine as running and holds every control while busy", () => {
    const root = mount({ detail: detailOf({ row: rowOf({ is_running: true }) }), isBusy: true });
    expect(root.querySelector("[data-run-now]")?.textContent).toBe("Running");
    for (const control of ["[data-run-now]", "[data-toggle]", "[data-change-schedule]", "[data-remove]"]) {
      expect(isDisabled(root, control), control).toBe(true);
    }
  });

  it("lists the runs newest first, each row a link to its page, with the restart note", () => {
    const root = mount({
      detail: detailOf({
        row: rowOf({ kind: "SKILL", skill: "digest", command: [], chat_id: "agent-1" }),
        runs: [
          runOf({ run_id: "run-2", when_text: "just now", outcome: "FAILED", outcome_text: "Failed", summary: "" }),
          runOf({
            run_id: "run-1",
            chat_id: "agent-1",
            warnings: ["RESTARTED_PREVIOUS_RUN"],
            trigger: "MANUAL",
          }),
        ],
      }),
    });
    expect(root.textContent).toContain("/digest");
    const rows = [...root.querySelectorAll<HTMLAnchorElement>("a[data-run]")];
    expect(rows.map((row) => row.getAttribute("data-run"))).toEqual(["run-2", "run-1"]);
    expect(rows.map((row) => row.getAttribute("href"))).toEqual(["/news-sync/runs/run-2", "/news-sync/runs/run-1"]);
    expect(rows[0].textContent).toContain("Failed");
    expect(rows[1].textContent).toContain("manual");
    expect(rows[1].textContent).toContain("3 headlines pulled");
    expect(rows[1].textContent).toContain("The previous run was still going and was cut off.");
    // A row is one link, so nothing inside it is another.
    expect(rows[1].querySelector("a")).toBeNull();
    // The routine's own chat is a plain link too: the app contract opens it where the workspace opens chats.
    expect(root.querySelector("[data-open-chat]")?.getAttribute("href")).toContain("?chat=agent-1");
  });

  it("says when nothing has run", () => {
    expect(mount({}).querySelector("[data-no-runs]")?.textContent).toBe("No runs yet.");
  });
});
