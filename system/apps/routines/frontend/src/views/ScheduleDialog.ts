/** The schedule editor: a number and a unit, a time, weekdays, a day of the month, or a single moment. */

import m from "mithril";

import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { MODAL_LABEL_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import { SegmentedControl } from "@imbue/workspace-ui/src/components/segmentedControl";

import {
  CADENCE_UNITS,
  WEEKDAYS,
  offersTimeOfDay,
  problemOf,
  sentenceOf,
  type ScheduleForm,
  type Weekday,
} from "../models/schedule";

export interface ScheduleDialogAttrs {
  form: ScheduleForm;
  isSaving: boolean;
  onChange: (form: ScheduleForm) => void;
  onSave: () => void;
  onCancel: () => void;
}

const FIELD_CLASS = inputClass({ extra: "w-auto" });

export function ScheduleDialog(): m.Component<ScheduleDialogAttrs> {
  const Mode = SegmentedControl<"every" | "once">();
  return {
    view({ attrs: { form, isSaving, onChange, onSave, onCancel } }) {
      const problem = problemOf(form);
      const sentence = sentenceOf(form);
      const set = (patch: Partial<ScheduleForm>): void => onChange({ ...form, ...patch });
      const toggleDay = (day: Weekday): void =>
        set({ days: form.days.includes(day) ? form.days.filter((other) => other !== day) : [...form.days, day] });
      return m(
        Modal,
        {
          title: "Change the schedule",
          onDismiss: onCancel,
          onEscape: onCancel,
          width: 460,
          card: { "data-schedule-dialog": "" },
          actions: [
            m(Button, { onclick: onCancel }, "Cancel"),
            m(
              Button,
              { variant: "primary", disabled: problem !== null || isSaving, onclick: onSave, "data-save": "" },
              isSaving ? "Saving" : "Save",
            ),
          ],
        },
        [
          m(
            "div",
            { class: "mb-3" },
            m(Mode, {
              label: "How it repeats",
              segments: [
                { value: "every", label: "Repeating" },
                { value: "once", label: "Once" },
              ],
              value: form.mode,
              onSelect: (mode) => set({ mode }),
            }),
          ),
          form.mode === "once"
            ? m("label", { class: "block" }, [
                m("span", { class: MODAL_LABEL_CLASS }, "When"),
                m("input", {
                  class: FIELD_CLASS,
                  type: "datetime-local",
                  value: form.once,
                  "data-once": "",
                  oninput: (event: Event) => set({ once: (event.target as HTMLInputElement).value }),
                }),
              ])
            : [
                m("div", { class: "flex flex-wrap items-center gap-2" }, [
                  m("span", { class: "type-body text-secondary" }, "Run every"),
                  m("input", {
                    class: inputClass({ extra: "w-20 text-right" }),
                    type: "number",
                    min: "1",
                    step: "1",
                    value: form.count,
                    "data-count": "",
                    oninput: (event: Event) => set({ count: (event.target as HTMLInputElement).value }),
                  }),
                  m(
                    "select",
                    {
                      class: FIELD_CLASS,
                      value: form.unit,
                      "data-unit": "",
                      onchange: (event: Event) =>
                        set({ unit: (event.target as HTMLSelectElement).value as ScheduleForm["unit"] }),
                    },
                    CADENCE_UNITS.map((unit) =>
                      m("option", { value: unit.value, selected: unit.value === form.unit }, unit.label),
                    ),
                  ),
                ]),
                offersTimeOfDay(form)
                  ? m("label", { class: "mt-3 block" }, [
                      m("span", { class: MODAL_LABEL_CLASS }, "At"),
                      m("input", {
                        class: FIELD_CLASS,
                        type: "time",
                        value: form.at,
                        "data-at": "",
                        oninput: (event: Event) => set({ at: (event.target as HTMLInputElement).value }),
                      }),
                    ])
                  : null,
                form.unit === "mo"
                  ? m("label", { class: "mt-3 block" }, [
                      m("span", { class: MODAL_LABEL_CLASS }, "On the day of the month"),
                      m("input", {
                        class: inputClass({ extra: "w-20" }),
                        type: "number",
                        min: "1",
                        max: "31",
                        value: form.dayOfMonth,
                        "data-day-of-month": "",
                        oninput: (event: Event) => set({ dayOfMonth: (event.target as HTMLInputElement).value }),
                      }),
                    ])
                  : null,
                m("div", { class: "mt-3" }, [
                  m("span", { class: MODAL_LABEL_CLASS }, "Only on"),
                  m(
                    "div",
                    { class: "flex flex-wrap gap-1" },
                    WEEKDAYS.map((day) =>
                      m(
                        Button,
                        {
                          sm: true,
                          variant: form.days.includes(day.value) ? "primary" : "secondary",
                          "data-day": day.value,
                          "aria-pressed": form.days.includes(day.value) ? "true" : "false",
                          onclick: () => toggleDay(day.value),
                        },
                        day.label.slice(0, 3),
                      ),
                    ),
                  ),
                ]),
              ],
          m(
            "p",
            { class: `type-helper mt-4 ${problem === null ? "text-secondary" : "text-danger"}`, "data-reads": "" },
            problem ?? (sentence === "" ? "" : `${sentence}.`),
          ),
        ],
      );
    },
  };
}
