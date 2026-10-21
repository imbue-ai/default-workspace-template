import { describe, expect, it } from "vitest";

import { definitionOf, scheduleFormOf } from "../testing/records";
import { changeOf, formOf, offersTimeOfDay, problemOf, sentenceOf, type ScheduleForm } from "./schedule";

const DEFINITION = definitionOf({
  every: "1d",
  at: "07:30",
  days: ["MON", "FRI"],
  command: null,
  skill: "inbox-digest",
});

/** The shared form with no time of day and no weekdays, which most cases here start from. */
function form(patch: Partial<ScheduleForm>): ScheduleForm {
  return scheduleFormOf({ at: "", days: [], ...patch });
}

describe("formOf", () => {
  it("opens on what the definition says", () => {
    expect(formOf(DEFINITION)).toEqual(form({ count: "1", unit: "d", at: "07:30", days: ["mon", "fri"] }));
  });

  it("opens a one-off on its moment", () => {
    const once = formOf({ ...DEFINITION, every: null, at: null, days: [], once: "2026-11-03T15:00:00" });
    expect(once.mode).toBe("once");
    expect(once.once).toBe("2026-11-03T15:00");
  });
});

describe("changeOf", () => {
  it("spells the cadence the way a definition does", () => {
    expect(changeOf(form({ count: "2", unit: "h", days: ["mon"] }))).toEqual({
      every: "2h",
      at: null,
      days: ["mon"],
      day_of_month: null,
      once: null,
    });
    expect(changeOf(form({ count: "1", unit: "mo", at: "00:00", dayOfMonth: "1" }))).toEqual({
      every: "1mo",
      at: "00:00",
      days: [],
      day_of_month: 1,
      once: null,
    });
    expect(changeOf(form({ mode: "once", once: "2026-11-03T15:00" }))).toEqual({
      every: null,
      at: null,
      days: [],
      day_of_month: null,
      once: "2026-11-03T15:00",
    });
  });
});

describe("problemOf", () => {
  it("names what cannot be saved", () => {
    expect(problemOf(form({ count: "0" }))).toMatch(/at least 1/);
    expect(problemOf(form({ count: "x" }))).toMatch(/whole number/);
    expect(problemOf(form({ unit: "mo", dayOfMonth: "32" }))).toMatch(/between 1 and 31/);
    expect(problemOf(form({ mode: "once", once: "" }))).toMatch(/date and a time/);
    expect(problemOf(form({ count: "1", unit: "d", at: "07:30" }))).toBeNull();
  });

  it("ignores a field the dialog hides, so a unit change never strands a value the user cannot clear", () => {
    // A daily-at-07:30 routine switched to hours: the time input is gone, and so is the time.
    const hourly = form({ count: "2", unit: "h", at: "07:30" });
    expect(problemOf(hourly)).toBeNull();
    expect(changeOf(hourly).at).toBeNull();
    expect(sentenceOf(hourly)).toBe("Every 2 hours");
    // A monthly-on-the-3rd routine switched to daily: the day input is gone, and so is the day.
    const daily = form({ unit: "d", dayOfMonth: "3", days: ["mon"] });
    expect(problemOf(daily)).toBeNull();
    expect(changeOf(daily).day_of_month).toBeNull();
    expect(changeOf(daily).days).toEqual(["mon"]);
    // A weekday routine switched to monthly keeps its weekdays: they go with any cadence.
    const monthly = form({ unit: "mo", days: ["mon"], dayOfMonth: "40" });
    expect(changeOf(monthly).days).toEqual(["mon"]);
    expect(problemOf(monthly)).toMatch(/between 1 and 31/);
  });

  it("offers a time of day only from a day up", () => {
    expect(offersTimeOfDay(form({ count: "30", unit: "m" }))).toBe(false);
    expect(offersTimeOfDay(form({ count: "1", unit: "d" }))).toBe(true);
    expect(offersTimeOfDay(form({ count: "24", unit: "h" }))).toBe(true);
  });
});

describe("sentenceOf", () => {
  it("reads as the list reads", () => {
    expect(sentenceOf(form({ count: "15", unit: "m" }))).toBe("Every 15 minutes");
    expect(sentenceOf(form({ count: "1", unit: "h" }))).toBe("Every hour");
    expect(sentenceOf(form({ count: "1", unit: "d", at: "07:30" }))).toBe("Every day at 7:30 am");
    expect(sentenceOf(form({ count: "1", unit: "d", at: "09:00", days: ["mon", "fri"] }))).toBe(
      "Every Monday and Friday at 9:00 am",
    );
    expect(sentenceOf(form({ count: "1", unit: "d", days: ["mon", "tue", "wed", "thu", "fri"] }))).toBe(
      "Every weekday",
    );
    expect(sentenceOf(form({ count: "2", unit: "w", days: ["mon", "tue", "wed", "thu", "fri"] }))).toBe(
      "Every 2 weeks on weekdays",
    );
    expect(sentenceOf(form({ count: "2", unit: "w", days: ["sat"] }))).toBe("Every 2 weeks on Saturday");
    expect(sentenceOf(form({ count: "1", unit: "mo", dayOfMonth: "1", at: "00:00" }))).toBe(
      "Monthly on the 1st at midnight",
    );
    expect(sentenceOf(form({ count: "2", unit: "mo", dayOfMonth: "22", at: "12:00" }))).toBe(
      "Every 2 months on the 22nd at noon",
    );
    expect(sentenceOf(form({ count: "1", unit: "mo", days: ["mon"] }))).toBe("Every month on Monday");
    expect(sentenceOf(form({ mode: "once", once: "2026-11-03T15:00" }))).toBe("Once, Nov 3 at 3:00 pm");
  });
});
