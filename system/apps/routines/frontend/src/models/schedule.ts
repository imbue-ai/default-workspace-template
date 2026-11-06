/**
 * The schedule editor's form: what the user types, turned into the API's schedule change, and the
 * sentence it will read as (the server says the final word; this spares a round trip).
 */

import type { RoutineDefinition, ScheduleChange } from "./api";

export type CadenceUnit = "m" | "h" | "d" | "w" | "mo";
export type Weekday = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";

export const WEEKDAYS: readonly { value: Weekday; label: string }[] = [
  { value: "mon", label: "Monday" },
  { value: "tue", label: "Tuesday" },
  { value: "wed", label: "Wednesday" },
  { value: "thu", label: "Thursday" },
  { value: "fri", label: "Friday" },
  { value: "sat", label: "Saturday" },
  { value: "sun", label: "Sunday" },
];

export const CADENCE_UNITS: readonly { value: CadenceUnit; label: string; one: string; seconds: number }[] = [
  { value: "m", label: "minutes", one: "minute", seconds: 60 },
  { value: "h", label: "hours", one: "hour", seconds: 3600 },
  { value: "d", label: "days", one: "day", seconds: 86400 },
  { value: "w", label: "weeks", one: "week", seconds: 7 * 86400 },
  { value: "mo", label: "months", one: "month", seconds: 30 * 86400 },
];

export interface ScheduleForm {
  mode: "every" | "once";
  count: string;
  unit: CadenceUnit;
  /** ``HH:MM`` or empty for any time. */
  at: string;
  days: Weekday[];
  /** The day a monthly routine is due, as typed; empty for the day the last run fell on. */
  dayOfMonth: string;
  /** ``YYYY-MM-DDTHH:MM`` for a one-off. */
  once: string;
}

const CADENCE_PATTERN = /^(\d+)(mo|m|h|d|w)$/;

/** The form a definition opens on. */
export function formOf(definition: RoutineDefinition): ScheduleForm {
  const match = definition.every === null ? null : CADENCE_PATTERN.exec(definition.every);
  return {
    mode: definition.once === null ? "every" : "once",
    count: match === null ? "1" : match[1],
    unit: match === null ? "d" : (match[2] as CadenceUnit),
    at: definition.at ?? "",
    days: definition.days.map((day) => day.toLowerCase() as Weekday),
    dayOfMonth: definition.day_of_month === null ? "" : String(definition.day_of_month),
    once: definition.once === null ? "" : definition.once.slice(0, 16),
  };
}

const CADENCE_UNIT_BY_VALUE: Record<CadenceUnit, (typeof CADENCE_UNITS)[number]> = Object.fromEntries(
  CADENCE_UNITS.map((unit) => [unit.value, unit]),
) as Record<CadenceUnit, (typeof CADENCE_UNITS)[number]>;

function unitOf(form: ScheduleForm): (typeof CADENCE_UNITS)[number] {
  return CADENCE_UNIT_BY_VALUE[form.unit];
}

/** Whether the form offers a time of day: only a cadence of a day or more has one. */
export function offersTimeOfDay(form: ScheduleForm): boolean {
  const count = Number(form.count);
  return form.mode === "every" && /^\d+$/.test(form.count.trim()) && count * unitOf(form).seconds >= 86400;
}

/** The form as the dialog shows it: a time of day under a day's cadence and a day of the month off a
 *  monthly one are hidden, and what is not shown is not part of the change (a value typed before the unit
 *  changed must not block the save or ride into the definition). Weekdays go with any cadence. */
export function shownFieldsOf(form: ScheduleForm): ScheduleForm {
  if (form.mode === "once") return form;
  return {
    ...form,
    at: offersTimeOfDay(form) ? form.at : "",
    dayOfMonth: form.unit === "mo" ? form.dayOfMonth : "",
  };
}

/** Why the form cannot be saved as it is, or null when it can. */
export function problemOf(form: ScheduleForm): string | null {
  if (form.mode === "once") {
    return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(form.once) ? null : "Pick a date and a time.";
  }
  const count = Number(form.count);
  if (!/^\d+$/.test(form.count.trim()) || count < 1) return "How often has to be a whole number, at least 1.";
  const { dayOfMonth } = shownFieldsOf(form);
  if (dayOfMonth !== "" && !(/^\d+$/.test(dayOfMonth) && Number(dayOfMonth) >= 1 && Number(dayOfMonth) <= 31)) {
    return "The day of the month has to be between 1 and 31.";
  }
  return null;
}

export function changeOf(form: ScheduleForm): ScheduleChange {
  if (form.mode === "once") {
    return { every: null, at: null, days: [], day_of_month: null, once: form.once };
  }
  const shown = shownFieldsOf(form);
  return {
    every: `${Number(shown.count)}${shown.unit}`,
    at: shown.at === "" ? null : shown.at,
    days: shown.days,
    day_of_month: shown.dayOfMonth === "" ? null : Number(shown.dayOfMonth),
    once: null,
  };
}

function timeText(at: string): string {
  const [hourText, minuteText] = at.split(":");
  const hour = Number(hourText);
  if (hour === 0 && minuteText === "00") return "midnight";
  if (hour === 12 && minuteText === "00") return "noon";
  return `${hour % 12 || 12}:${minuteText} ${hour < 12 ? "am" : "pm"}`;
}

function ordinal(day: number): string {
  const suffix =
    day % 100 >= 10 && day % 100 <= 20
      ? "th"
      : (({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[day % 10] ?? "th");
  return `${day}${suffix}`;
}

function isWorkingWeek(days: Weekday[]): boolean {
  return new Set(days).size === 5 && !days.includes("sat") && !days.includes("sun");
}

function daysText(days: Weekday[]): string {
  if (isWorkingWeek(days)) return "weekdays";
  const names = WEEKDAYS.filter((day) => days.includes(day.value)).map((day) => day.label);
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** The sentence the schedule will read as, in the words the list uses. */
export function sentenceOf(typed: ScheduleForm): string {
  if (typed.mode === "once") {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(typed.once)) return "";
    const date = new Date(typed.once);
    const month = date.toLocaleString("en-US", { month: "short" });
    return `Once, ${month} ${date.getDate()} at ${timeText(typed.once.slice(11, 16))}`;
  }
  const form = shownFieldsOf(typed);
  const count = Number(form.count);
  const unit = unitOf(form);
  let head: string;
  if (form.unit === "mo" && form.dayOfMonth !== "") {
    head =
      count === 1
        ? `Monthly on the ${ordinal(Number(form.dayOfMonth))}`
        : `Every ${count} ${unit.label} on the ${ordinal(Number(form.dayOfMonth))}`;
  } else if (form.days.length > 0 && form.unit === "d" && count === 1) {
    head = isWorkingWeek(form.days) ? "Every weekday" : `Every ${daysText(form.days)}`;
  } else {
    head = count === 1 ? `Every ${unit.one}` : `Every ${count} ${unit.label}`;
    if (form.days.length > 0) head = `${head} on ${daysText(form.days)}`;
  }
  return form.at === "" ? head : `${head} at ${timeText(form.at)}`;
}
