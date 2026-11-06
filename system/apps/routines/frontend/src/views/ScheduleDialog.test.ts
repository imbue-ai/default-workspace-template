// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import { scheduleFormOf } from "../testing/records";
import { ScheduleDialog, type ScheduleDialogAttrs } from "./ScheduleDialog";

afterEach(() => unmountViews());

function mount(attrs: Partial<ScheduleDialogAttrs>): HTMLElement {
  return mountView(() =>
    m(ScheduleDialog, {
      form: scheduleFormOf(),
      isSaving: false,
      onChange: () => undefined,
      onSave: () => undefined,
      onCancel: () => undefined,
      ...attrs,
    }),
  );
}

describe("ScheduleDialog", () => {
  it("previews the sentence the schedule will read as and saves on the button", () => {
    const onSave = vi.fn();
    const root = mount({ onSave });
    expect(root.querySelector("[data-reads]")?.textContent).toBe("Every Monday at 7:30 am.");
    expect(root.querySelector("[data-at]")).not.toBeNull();
    expect(root.querySelector("[data-day-of-month]")).toBeNull();
    expect(root.querySelector("[data-day='mon']")?.getAttribute("aria-pressed")).toBe("true");
    expect(root.querySelector("[data-day='tue']")?.getAttribute("aria-pressed")).toBe("false");
    root.querySelector<HTMLButtonElement>("[data-save]")!.click();
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("hides the time of day under a day's cadence and offers the day of the month on a monthly one", () => {
    const minutes = mount({ form: scheduleFormOf({ unit: "m", count: "15" }) });
    expect(minutes.querySelector("[data-at]")).toBeNull();
    expect(minutes.querySelector("[data-reads]")?.textContent).toBe("Every 15 minutes on Monday.");

    unmountViews();
    const monthly = mount({ form: scheduleFormOf({ unit: "mo", dayOfMonth: "1" }) });
    // Weekdays go with any cadence, so the picker stays.
    expect(monthly.querySelector("[data-day='mon']")?.getAttribute("aria-pressed")).toBe("true");
    expect(monthly.querySelector("[data-day-of-month]")).not.toBeNull();
    expect(monthly.querySelector("[data-reads]")?.textContent).toBe("Monthly on the 1st at 7:30 am.");
  });

  it("refuses to save a form with a problem and says what it is", () => {
    const root = mount({ form: scheduleFormOf({ count: "0" }) });
    expect(root.querySelector("[data-reads]")?.textContent).toBe("How often has to be a whole number, at least 1.");
    expect(root.querySelector<HTMLButtonElement>("[data-save]")!.disabled).toBe(true);
  });

  it("hands every change back as a new form", () => {
    const onChange = vi.fn();
    const root = mount({ onChange });
    root.querySelector<HTMLButtonElement>("[data-day='fri']")!.click();
    expect(onChange).toHaveBeenLastCalledWith(scheduleFormOf({ days: ["mon", "fri"] }));
    const count = root.querySelector<HTMLInputElement>("[data-count]")!;
    count.value = "2";
    count.dispatchEvent(new Event("input", { bubbles: true }));
    expect(onChange).toHaveBeenLastCalledWith(scheduleFormOf({ count: "2" }));
  });

  it("asks for a moment alone when the routine runs once", () => {
    const root = mount({ form: scheduleFormOf({ mode: "once", once: "2026-11-03T15:00" }) });
    expect(root.querySelector("[data-once]")).not.toBeNull();
    expect(root.querySelector("[data-count]")).toBeNull();
    expect(root.querySelector("[data-reads]")?.textContent).toBe("Once, Nov 3 at 3:00 pm.");
  });
});
