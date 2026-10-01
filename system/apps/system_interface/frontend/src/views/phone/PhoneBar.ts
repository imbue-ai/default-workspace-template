/**
 * The phone's bar (plan-phone-interface.md): home, the pill, and plus, above the safe-area inset. Home is disabled
 * on the home grid; the pill names what is on screen (the workspace on the home grid, the avatar and the app's
 * name for the pinned window, else the window's icon and title) with the count of other open windows, opens the
 * windows sheet on a tap and the shown window's menu on a long press; plus opens the start sheet.
 */

import m from "mithril";
import type { AvatarState } from "../../reducers/desktopState";
import type { PhonePill } from "../../reducers/phone";
import { glyph } from "../glyphs";
import { longPressAttrs } from "./longPress";
import { windowIcon } from "./windowIcon";

const CONTROL_GLYPH_SIZE = 26;
const PILL_GLYPH_SIZE = 32;

export interface PhoneBarAttrs {
  readonly pill: PhonePill;
  /** Open windows other than the pinned one; the pill shows no count at zero. */
  readonly count: number;
  readonly avatar: AvatarState;
  readonly onHome: () => void;
  readonly onPill: () => void;
  readonly onPillLongPress: (target: HTMLElement) => void;
  readonly onNew: () => void;
  readonly thresholdPx: number;
}

// Bare glyphs at rest; a circle comes up under a press.
const CONTROL_CLASS =
  "phone-control relative flex h-(--desk-phone-control-height) w-(--desk-phone-control-width) shrink-0 " +
  "items-center justify-center text-primary disabled:text-faint " +
  "before:absolute before:size-(--desk-phone-control-height) before:rounded-full before:content-[''] " +
  "not-disabled:active:before:bg-fill-active [&>svg]:relative";

function pillIcon(pill: PhonePill, avatar: AvatarState): m.Children {
  if (pill.kind === "home") return null;
  return windowIcon(pill.window, pill.app, avatar, "phone-pill-icon size-(--desk-phone-pill-icon)", PILL_GLYPH_SIZE);
}

export const PhoneBar: m.Component<PhoneBarAttrs> = {
  view(vnode) {
    const { pill, count, avatar, onHome, onPill, onPillLongPress, onNew, thresholdPx } = vnode.attrs;
    const isHome = pill.kind === "home";
    return m(
      "nav",
      {
        "data-phone-bar": "",
        class:
          "phone-bar relative z-(--z-content) flex shrink-0 items-center justify-center gap-2 border-t " +
          "border-default bg-page/90 px-(--desk-phone-bar-padding) pt-(--desk-phone-bar-padding) " +
          "pb-(--desk-phone-safe-bottom) backdrop-blur-xl",
      },
      [
        m(
          "button",
          {
            type: "button",
            "data-phone-home": "",
            "aria-label": "Home",
            disabled: isHome,
            class: CONTROL_CLASS,
            onclick: onHome,
          },
          m.trust(glyph("home", CONTROL_GLYPH_SIZE)),
        ),
        m(
          "button",
          {
            type: "button",
            "data-phone-pill": pill.kind === "home" ? "home" : pill.window.id,
            "aria-label": `${pill.title}: open windows`,
            class:
              "phone-pill flex h-(--desk-phone-pill-height) max-w-(--desk-phone-pill-max-width) min-w-0 flex-1 " +
              "items-center gap-2 rounded-full bg-fill-hover pr-2 text-left select-none active:bg-fill-active " +
              (isHome ? "pl-4" : "pl-2"),
            ...longPressAttrs({ onTap: onPill, onLongPress: onPillLongPress, thresholdPx }),
          },
          [
            pillIcon(pill, avatar),
            m("span", { class: "phone-pill-title min-w-0 flex-1 truncate text-sm font-semibold" }, pill.title),
            count === 0
              ? null
              : m(
                  "span",
                  {
                    "data-phone-count": String(count),
                    class:
                      "phone-pill-count ml-auto flex h-6.5 min-w-6.5 shrink-0 items-center justify-center " +
                      "rounded-full bg-fill-hover px-1.5 text-xs font-bold text-secondary",
                  },
                  String(count),
                ),
          ],
        ),
        m(
          "button",
          {
            type: "button",
            "data-phone-new": "",
            "aria-label": "Open an app or send a message",
            class: CONTROL_CLASS,
            onclick: onNew,
          },
          m.trust(glyph("plus", CONTROL_GLYPH_SIZE)),
        ),
      ],
    );
  },
};
