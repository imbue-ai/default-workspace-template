/**
 * The small pieces the tiles, the detail pages and the dialogs share: an app's icon, its room badge, the meter of
 * what it needs against what is free, its install progress, a label and value row, a figure against what is free,
 * an outside link, the way back, the warning and refusal lines, and a dialog card's attributes.
 */

import m from "mithril";
import { badgeClass } from "@imbue/workspace-ui/src/components/Badge";
import type { BadgeTone } from "@imbue/workspace-ui/src/components/Badge";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { InstallStatus, RoomVerdict } from "../models/Store";
import { VERDICT_LABELS, formatMb } from "../room";

const VERDICT_TONES: Record<RoomVerdict, BadgeTone> = { FITS: "success", TIGHT: "warning", WONT_FIT: "danger" };
const LINK_GLYPH_SIZE = 14;
const BACK_GLYPH_SIZE = 16;

/** The steps an install shows, in order. */
const INSTALL_STAGES: readonly { status: InstallStatus; label: string }[] = [
  { status: "QUEUED", label: "Waiting" },
  { status: "DOWNLOADING", label: "Downloading" },
  { status: "CONFIGURING", label: "Setting up" },
  { status: "STARTING", label: "Starting" },
];

export function verdictBadge(verdict: RoomVerdict): m.Vnode {
  return m("span", { class: badgeClass(VERDICT_TONES[verdict]), "data-verdict": verdict }, VERDICT_LABELS[verdict]);
}

/** An app's icon at ``size`` px, drawn from a URL (an <img>, so an icon's markup never runs in the page). */
export function appIcon(src: string, size: number): m.Vnode {
  return src === ""
    ? m("span", {
        class: "block shrink-0 bg-surface-secondary",
        style: `width:${size}px;height:${size}px;border-radius:${size * 0.32}px`,
        "aria-hidden": "true",
      })
    : m("img", { src, alt: "", width: size, height: size, class: "block shrink-0", draggable: false });
}

/** A thin bar: how much of what is free the app would take. */
export function roomMeter(neededMb: number, freeMb: number): m.Vnode {
  const isOver = neededMb > freeMb;
  const percent = isOver || freeMb <= 0 ? 100 : Math.max(2, (neededMb / freeMb) * 100);
  return m(
    "div",
    { class: "mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-surface-secondary", "data-meter": "" },
    m("div", { class: `h-full rounded-full ${isOver ? "bg-danger" : "bg-accent"}`, style: `width:${percent}%` }),
  );
}

/** Where an install stands, as a label and four segments; an uninstall is one line. */
export function installProgress(status: InstallStatus, logHref: string | null): m.Vnode {
  const logLink =
    logHref === null
      ? null
      : m(
          "a",
          {
            class: "text-secondary hover:text-primary",
            href: logHref,
            target: "_blank",
            rel: "noopener noreferrer",
            // Inside a tile, which opens the app's page on a click: the link opens only the log.
            onclick: (event: Event) => event.stopPropagation(),
          },
          "Install log",
        );
  if (status === "UNINSTALLING") {
    return m("div", { class: "flex items-center justify-between type-helper", "data-install-stage": status }, [
      m("span", { class: "flex items-center gap-2 text-primary" }, [
        m("span", { class: "spinner spinner--sm" }),
        "Uninstalling…",
      ]),
      logLink,
    ]);
  }
  const current = Math.max(
    0,
    INSTALL_STAGES.findIndex((entry) => entry.status === status),
  );
  return m("div", { class: "flex flex-col gap-1.5", "data-install-stage": status }, [
    m("div", { class: "flex items-center justify-between type-helper" }, [
      m("span", { class: "flex items-center gap-2 text-primary" }, [
        m("span", { class: "spinner spinner--sm" }),
        `${INSTALL_STAGES[current].label}…`,
      ]),
      logLink,
    ]),
    m(
      "div",
      { class: "flex gap-1" },
      INSTALL_STAGES.map((entry, index) =>
        m("span", {
          class: `h-1 flex-1 rounded-full ${
            index < current ? "bg-accent" : index === current ? "bg-accent/50" : "bg-surface-secondary"
          }`,
          title: entry.label,
        }),
      ),
    ),
  ]);
}

/** One label and value row of a ``<dl>``; ``hook`` marks the value for tests (``data-<hook>``). */
export function fact(label: string, value: m.Children, hook?: string): m.Vnode[] {
  return [
    m("dt", { class: "type-helper text-faint" }, label),
    m("dd", { class: "m-0 type-body text-primary", ...(hook === undefined ? {} : { [`data-${hook}`]: "" }) }, value),
  ];
}

/** A figure, then what the workspace has of it in a quieter voice: "about 100 MB (2.0 GB free)". */
export function againstRoom(text: string, freeMb: number, freeWord: string): m.Children {
  return [`${text} `, m("span", { class: "type-helper text-faint" }, `(${formatMb(freeMb)} ${freeWord})`)];
}

/** A link that opens outside the store, in a new tab, marked with the external-link glyph. */
export function externalLink(href: string, label: string, extraClass = ""): m.Vnode {
  return m(
    "a",
    {
      class: `inline-flex items-center gap-1.5 type-helper text-secondary hover:text-primary ${extraClass}`,
      href,
      target: "_blank",
      rel: "noopener noreferrer",
    },
    [m("span", label), m.trust(icon("external-link", { size: LINK_GLYPH_SIZE }))],
  );
}

/** The way back from a detail page to the lists. */
export function backButton(onBack: () => void): m.Vnode {
  return m(Button, { variant: "ghost", sm: true, extra: "app-store-back", onclick: onBack }, [
    m.trust(icon("chevron-left", { size: BACK_GLYPH_SIZE })),
    m("span", "Back"),
  ]);
}

/** The tinted line that says whose code this is and what it can reach. */
export function warningNote(text: string, marginClass = "mb-4"): m.Vnode {
  return m(
    "p",
    {
      class: `m-0 ${marginClass} rounded-lg bg-warning-surface px-3 py-2 type-helper text-primary`,
      "data-warning": "",
    },
    text,
  );
}

/** Why the store refused, in the dialog that asked; nothing when it has not. */
export function refusal(reason: string | null): m.Vnode | null {
  return reason === null ? null : m("p", { class: "m-0 mb-4 type-helper text-danger", "data-refusal": "" }, reason);
}

/** A modal dialog card's attributes: named by its title for assistive tech, marked ``data-dialog=<kind>``. */
export function dialogCard(kind: string, title: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { role: "dialog", "aria-modal": "true", "aria-label": title, "data-dialog": kind, ...extra };
}

/** An ``oncreate`` that puts the keyboard focus on the element once it is drawn: a dialog's safe way out, so Enter or
 *  Space never confirms what the person has not read yet. */
export function focusOnCreate({ dom }: { dom: Element }): void {
  (dom as HTMLElement).focus();
}
