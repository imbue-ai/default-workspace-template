/**
 * A person's card (the multiplayer spec, section 6.3): the picture, the name ("(you)", "(owner)"), the status line
 * with its dot, then, when connected, what they are looking at (the window at the top of the stack on the active
 * desktop of their newest client), then their desktops (the ones they own, section 8.1), one row each, the one they
 * are on marked "There now" and the one the viewer is on marked "Viewing", a click viewing it (section 8.3); your
 * own card lists your own desktops (a click switches) and ends with "New desktop". A visiting agent's card shows
 * its name, "Visiting agent", and the status line, and no desktops. On an unshared workspace the owner's own entry
 * is a neutral silhouette and the card reads "You".
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { desktopsOf, isClientOf } from "../../model/Ownership";
import type { AppRecord, ClientRecord, Desktop, RosterParticipant, WindowRecord } from "../../model/records";
import { OWNER_KEY, presenceStateOf, statusLineOf } from "../../model/Roster";
import { appGlyph } from "../glyphs";
import { TrayAvatar } from "./TrayAvatar";
import { TrayCardFrame } from "./TrayCard";
import type { TrayCardFrameAttrs } from "./TrayCard";

const CARD_PICTURE_SIZE_PX = 32;
const LOOKING_AT_ICON_SIZE = 16;
const MONITOR_GLYPH_SIZE = 14;

/** What a person is looking at: the window at the top of the stack on the active desktop of their newest connected
 *  client; null when they have no window open or no client of theirs is known. */
export interface LookingAt {
  readonly window: WindowRecord;
  readonly app: AppRecord | undefined;
  readonly desktop: Desktop;
}

/** A person's newest client: the most recently seen connected one, else the most recently seen; undefined when no
 *  client of theirs is known. */
function newestClientOf(entry: RosterParticipant, clients: readonly ClientRecord[]): ClientRecord | undefined {
  const own = clients
    .filter((client) => isClientOf(entry, client))
    .sort((left, right) => right.last_seen.localeCompare(left.last_seen));
  return own.find((client) => client.is_connected) ?? own[0];
}

/** The desktop a person is on: their newest connected client's active desktop; null when no connected client of
 *  theirs is known. */
export function desktopOfPerson(entry: RosterParticipant, clients: readonly ClientRecord[]): string | null {
  const newest = newestClientOf(entry, clients);
  return newest === undefined || !newest.is_connected ? null : newest.active_desktop;
}

export function lookingAtOf(
  entry: RosterParticipant,
  clients: readonly ClientRecord[],
  desktops: readonly Desktop[],
  apps: readonly AppRecord[],
): LookingAt | null {
  const newest = newestClientOf(entry, clients);
  if (newest === undefined || newest.active_desktop === null) return null;
  const desktop = desktops.find((candidate) => candidate.id === newest.active_desktop);
  if (desktop === undefined) return null;
  const [windowId] = newest.shown;
  const window = windowId === undefined ? undefined : desktop.windows.find((candidate) => candidate.id === windowId);
  if (window === undefined) return null;
  return { window, app: apps.find((app) => app.name === window.app), desktop };
}

export interface PersonCardAttrs extends Omit<TrayCardFrameAttrs, "label"> {
  readonly entry: RosterParticipant;
  readonly isOwn: boolean;
  readonly now: Date;
  readonly clients: readonly ClientRecord[];
  readonly desktops: readonly Desktop[];
  readonly apps: readonly AppRecord[];
  readonly activeDesktopId: string | null;
  /** A click on a desktop row: a switch onto your own, a viewing of anyone else's. */
  readonly onOpenDesktop: (desktopId: string) => void;
  readonly onNewDesktop: () => void;
}

function statusDotClass(state: ReturnType<typeof presenceStateOf>): string {
  switch (state) {
    case "active":
      return "bg-success";
    case "idle":
      return "bg-warning";
    case "offline":
      return "bg-faint";
    case "invited":
      return "border border-dashed border-strong bg-transparent";
  }
}

export const PersonCard: m.Component<PersonCardAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    const { entry, isOwn, now } = attrs;
    const isAnonymousOwner = entry.key === OWNER_KEY;
    const name = isAnonymousOwner ? "You" : entry.display_name + (isOwn ? " (you)" : entry.is_owner ? " (owner)" : "");
    const state = presenceStateOf(entry, now);
    const isVisitingAgent = entry.kind === "visiting_agent";
    const looking =
      state === "offline" || state === "invited" || isVisitingAgent
        ? null
        : lookingAtOf(entry, attrs.clients, attrs.desktops, attrs.apps);
    const desktops = isVisitingAgent || entry.kind === "invited" ? [] : desktopsOf(entry, attrs.desktops);
    // Where they are now, as the clients were read when the card opened; your own card says where you are
    // through "Viewing" alone.
    const theirDesktopId = isOwn || state === "offline" ? null : desktopOfPerson(entry, attrs.clients);
    return m(TrayCardFrame, { anchor: attrs.anchor, label: name, onEnter: attrs.onEnter, onLeave: attrs.onLeave }, [
      m("div", { class: "flex items-center gap-2" }, [
        m(TrayAvatar, { entry, sizePx: CARD_PICTURE_SIZE_PX, isFaded: false }),
        m("div", { class: "flex min-w-0 flex-col" }, [
          m("div", { class: "truncate font-semibold", "data-tray-card-name": "" }, name),
          m("div", { class: "flex items-center gap-1.5 text-(length:--font-size-helper) text-secondary" }, [
            m("span", { class: `inline-block size-2 shrink-0 rounded-full ${statusDotClass(state)}` }),
            m(
              "span",
              { "data-tray-card-status": "" },
              isVisitingAgent ? `Visiting agent · ${statusLineOf(entry, now)}` : statusLineOf(entry, now),
            ),
          ]),
        ]),
      ]),
      looking === null
        ? null
        : m("div", { class: "flex flex-col gap-1", "data-tray-card-looking": looking.window.id }, [
            m(
              "div",
              { class: "text-(length:--font-size-helper) text-faint" },
              state === "idle" ? "Last seen in" : "Looking at",
            ),
            m("div", { class: "flex items-center gap-1.5" }, [
              m(
                "span",
                { class: "flex size-4 shrink-0 items-center [&>svg]:size-full" },
                m.trust(appGlyph(looking.app, LOOKING_AT_ICON_SIZE)),
              ),
              m(
                "span",
                { class: "truncate" },
                looking.window.title !== "" ? looking.window.title : (looking.app?.display_name ?? looking.window.app),
              ),
            ]),
          ]),
      desktops.length === 0 && !isOwn
        ? null
        : m("div", { class: "flex flex-col gap-0.5", "data-tray-card-desktops": "" }, [
            m(
              "div",
              { class: "text-(length:--font-size-helper) text-faint" },
              isOwn ? "Your desktops" : `${entry.display_name}'s desktops`,
            ),
            desktops.map((desktop) =>
              m(
                "button",
                {
                  key: desktop.id,
                  type: "button",
                  "data-tray-card-desktop": desktop.id,
                  class:
                    "flex cursor-pointer items-center gap-1.5 rounded-md border-0 bg-transparent px-1 py-0.5 text-left text-primary hover:bg-fill-hover",
                  onclick: () => attrs.onOpenDesktop(desktop.id),
                },
                [
                  m(
                    "span",
                    { class: "flex shrink-0 items-center text-secondary" },
                    m.trust(icon("columns", { size: MONITOR_GLYPH_SIZE })),
                  ),
                  m("span", { class: "min-w-0 flex-1 truncate" }, desktop.name),
                  desktop.id === theirDesktopId
                    ? m(
                        "span",
                        {
                          class: "flex items-center gap-1 text-(length:--font-size-helper) text-faint",
                          "data-tray-card-there": desktop.id,
                        },
                        [
                          m("span", {
                            class: "inline-block size-1.5 shrink-0 rounded-full",
                            style: { backgroundColor: entry.color },
                          }),
                          "There now",
                        ],
                      )
                    : null,
                  desktop.id === attrs.activeDesktopId
                    ? m("span", { class: "text-(length:--font-size-helper) text-faint" }, "Viewing")
                    : null,
                ],
              ),
            ),
            isOwn
              ? m(
                  "button",
                  {
                    type: "button",
                    "data-tray-card-new-desktop": "",
                    class:
                      "flex cursor-pointer items-center gap-1.5 rounded-md border-0 bg-transparent px-1 py-0.5 text-left text-secondary hover:bg-fill-hover",
                    onclick: attrs.onNewDesktop,
                  },
                  [
                    m(
                      "span",
                      { class: "flex shrink-0 items-center" },
                      m.trust(icon("plus", { size: MONITOR_GLYPH_SIZE })),
                    ),
                    "New desktop",
                  ],
                )
              : null,
          ]),
    ]);
  },
};
