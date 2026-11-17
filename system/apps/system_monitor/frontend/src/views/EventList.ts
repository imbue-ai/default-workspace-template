/**
 * The lists of the last 24 hours, each line its time, what happened, and an action: the overview's events (a stop
 * or a busy stretch, with a way to its view), the CPU view's busy stretches, and the memory view's stops, whose full
 * command opens under the line.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";

import { Button, buttonClass, buttonPartAttrs } from "@imbue/workspace-ui/src/components/Button";

import type { EventRecord, StopRecord } from "../models/api";
import { capitalized, when } from "../models/format";

const LINE_CLASS = "grid grid-cols-[64px_1fr_auto] gap-x-3 border-b border-subtle py-2 type-body-dense";
const EMPTY_CLASS = "border-b border-subtle py-2 type-body-dense text-body-content-secondary";
const TIME_CLASS = "text-body-content-secondary tabular-nums";

function time(iso: string): m.Vnode {
  return m("time", { class: TIME_CLASS, datetime: iso }, when(iso));
}

export interface EventListAttrs {
  events: readonly EventRecord[];
  emptyText: string;
  /** Offer a way from each event to the view it belongs to (the overview's list). */
  hasDetailsLinks?: boolean;
}

export const EventList: m.Component<EventListAttrs> = {
  view({ attrs: { events, emptyText, hasDetailsLinks = false } }) {
    if (!events.length) return m("ul", { class: "events" }, m("li", { class: EMPTY_CLASS }, emptyText));
    return m(
      "ul",
      { class: "events" },
      events.map((event) => {
        const causes = event.top?.length
          ? m(
              "small",
              { class: "block type-helper text-body-content-tertiary" },
              event.top.map((cause) => `${cause.name} ${Math.round(cause.share * 100)}%`).join(" · "),
            )
          : null;
        return m("li", { class: LINE_CLASS }, [
          time(event.at),
          m("span", { class: "text-body-content" }, [event.what, causes]),
          hasDetailsLinks
            ? m(
                "a",
                {
                  href: event.kind === "stop" ? "/memory" : "/cpu",
                  class: buttonClass("ghost", { sm: true, extra: "-my-[5px] -mr-2 self-start no-underline" }),
                  ...buttonPartAttrs("ghost", false),
                },
                "Details",
              )
            : m("span"),
        ]);
      }),
    );
  },
};

export interface StopListAttrs {
  stops: readonly StopRecord[];
  /** Which stops' Details are open, by when they happened. */
  openStops: ReadonlySet<string>;
  onToggle: (at: string) => void;
}

export const StopList: m.Component<StopListAttrs> = {
  view({ attrs: { stops, openStops, onToggle } }) {
    if (!stops.length) {
      return m("ul", { class: "stops" }, m("li", { class: EMPTY_CLASS }, "Nothing stopped in the last 24 hours."));
    }
    return m(
      "ul",
      { class: "stops" },
      stops.map((stop, index) => {
        const isOpen = openStops.has(stop.at);
        const panelId = `stop-${index}`;
        return m("li", { class: LINE_CLASS }, [
          time(stop.at),
          m("span", { class: "text-body-content" }, capitalized(stop.what)),
          stop.command === null
            ? null
            : [
                m(
                  Button,
                  {
                    variant: "ghost",
                    sm: true,
                    extra: "-my-[5px] -mr-2 self-start",
                    "aria-expanded": isOpen ? "true" : "false",
                    "aria-controls": panelId,
                    onclick: () => onToggle(stop.at),
                  },
                  isOpen ? "Hide details" : "Details",
                ),
                m(
                  "div",
                  {
                    id: panelId,
                    hidden: !isOpen,
                    class:
                      "stop-details col-[2/-1] mt-2 flex flex-col gap-2.5 rounded-md bg-body-surface-secondary px-3 py-2.5",
                  },
                  m("div", [
                    m("div", { class: "mb-0.5 type-label text-body-content-secondary" }, "Command"),
                    m("pre", { class: "m-0 type-code-small whitespace-pre-wrap wrap-anywhere" }, stop.command),
                  ]),
                ),
              ],
        ]);
      }),
    );
  },
};
