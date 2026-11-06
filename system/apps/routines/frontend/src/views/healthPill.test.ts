// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import { afterEach, describe, expect, it } from "vitest";

import type { Health } from "../models/api";
import { rowOf } from "../testing/records";
import { healthPill, toneOf } from "./healthPill";

afterEach(() => unmountViews());

describe("healthPill", () => {
  it("shows a run in flight as running, unless the routine is waiting on the user", () => {
    const running = mountView(() =>
      healthPill(rowOf({ is_running: true, health: "FAILING", health_text: "Failing" })),
    );
    expect(running.querySelector("[data-health]")?.getAttribute("data-health")).toBe("RUNNING");
    expect(running.textContent).toContain("Running");

    const blocked = mountView(() =>
      healthPill(rowOf({ is_running: true, health: "NEEDS_USER", health_text: "Needs you" })),
    );
    expect(blocked.querySelector("[data-health]")?.getAttribute("data-health")).toBe("NEEDS_USER");
    expect(blocked.textContent).toBe("Needs you");
  });

  it.each<[Health, string]>([
    ["WORKING", "success"],
    ["DONE", "success"],
    ["FAILING", "danger"],
    ["BROKEN", "danger"],
    ["OVERDUE", "warning"],
    ["NEEDS_USER", "warning"],
    ["OFF", "neutral"],
    ["NOT_RUN_YET", "neutral"],
  ])("colours %s as %s", (health, tone) => {
    expect(toneOf(health)).toBe(tone);
  });
});
