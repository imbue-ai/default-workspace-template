/**
 * The composer's model/provider menu: which PROVIDER this chat runs on, and which model on it.
 *
 * The provider leads, because with several accounts signed in it is the first thing worth
 * knowing about a chat.
 *
 * Everything it shows is data: the static per-harness catalog from HarnessCatalog.ts, the
 * chat's live `model_choice` pushed onto the chats store, and its account id resolved
 * against the account list. Which rows show is decided by the matched catalog option (effort
 * iff the model declares more than one; fast iff it supports it); the switch mode decides
 * whether they are interactive.
 *
 * The provider row is the one that always renders. A provider is a property of the ACCOUNT,
 * not of the model, so it survives all three of the states in which there is no model to show.
 * While a switch is armed, or being carried out, the menu describes the TARGET instead, since
 * that is what the chat's messages run on. A chat with no agent yet (awaiting its first send, or
 * being created) has no menu: the chip names the account the chat starts on, and with no
 * provider signed in at all it says so and opens the provider chooser.
 *
 * How the menu opens, closes and grows its submenus is the workspace `Menu`'s
 * (`components/menu`), not this file's. What this file owns is the rows and the data behind
 * them.
 *
 * Auto-compact belongs to the HARNESS rather than the model: shown whenever the chat's harness
 * can be compacted while idle, whatever model it runs.
 *
 * In the phone layout (`isCompact`) the same menu opens from a settings button at the left of
 * the composer instead of the chip under it, and draws the same rows on a sliding track
 * (`slidingMenuTrack.ts`): submenus slide in over the card instead of flying out beside it,
 * effort is a row of segments instead of a slider, and the Source view switch is a row of its
 * own: the phone's under-bar carries the switch only while the terminal face is up, to turn back.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { getChatById, getProvisionalChat } from "../models/Chats";
import type { ChatSnapshot } from "../models/Chats";
import type { CatalogModelOption, HarnessCatalog } from "../models/HarnessCatalog";
import { effortInEffect, ensureHarnessCatalogs, getHarnessCatalog, startingEffort } from "../models/HarnessCatalog";
import type { ChatFastModeState } from "../models/FastMode";
import {
  FAST_MODES,
  FAST_MODE_LABELS,
  ensureFastModeState,
  fastModeDetail,
  fastModeLabel,
  getFastModeState,
} from "../models/FastMode";
import type { ChatAutocompactState } from "../models/Autocompact";
import {
  autocompactLabel,
  ensureAutocompactState,
  getAutocompactState,
  setAutocompactState,
} from "../models/Autocompact";
import {
  DEFAULT_CHAT_SETTINGS,
  ensureChatSettings,
  getChatSettings,
  updateChatSettings,
} from "../models/ChatSettings";
import type { ChatSettings } from "../models/ChatSettings";
import { getEventsForChat } from "../models/Response";
import { chooseFastMode } from "./fast-mode-limit";
import { changedAxes, effectiveChoice, setModelChoice } from "../models/ModelSettings";
import type { ModelIdentity } from "../models/ModelSettings";
import {
  getPendingPick,
  isSwitchTarget,
  nextSendSwitchTarget,
  pendingSwitchTarget,
  switchKind,
  underwaySwitchTarget,
} from "../models/PendingLane";
import {
  accountForAgent,
  accountForFirstSend,
  areAccountsLoaded,
  getAccounts,
  getDefaultAccountId,
  isAccountSignedOut,
  openProviderChooser,
} from "../models/Providers";
import { beginSwitchTo, beginSwitchToAccountId, openSwitchDialog, takeBackSwitch } from "./SwitchDialog";
import type { ProviderAccount } from "../models/Providers";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import {
  createMenu,
  menuDividerClass,
  menuRowClass,
  startTruncated,
  type MenuRow,
} from "@imbue/workspace-ui/src/components/menu";
import { slidersGlyph } from "../glyphs";
import { accountRow, emptyAccountRowState } from "./accountRow";
import { capitalizeEffort, modelPickLabel } from "./model-pick-label";
import * as css from "./modelProviderMenuStyles";
import { createSlidingMenuTrack } from "./slidingMenuTrack";

/** Shown on a read-only harness's rows. agy's `/model` is an interactive TUI with no
 *  scriptable form, so the menu cannot drive it -- and says where the user can. */
const READ_ONLY_TOOLTIP = "To change the model or effort, run /model or /effort in the agent terminal.";

/** The effort to carry when switching to `option`: keep the current one if the new model declares
 *  it, else the effort the model starts at. */
function clampEffort(option: CatalogModelOption, currentEffort: string | null): string | null {
  if (currentEffort !== null && option.efforts.some((effort) => effort.level === currentEffort)) {
    return currentEffort;
  }
  return startingEffort(option);
}

/** The model a switch in progress is taking the chat to, as the chip reads it; null when the chat
 *  is not converging, the switch failed, or it picked no model, all of which leave the chip on the
 *  live choice. A failed switch still carries its pick, for the retry to rerun the model step from,
 *  but never applied it.
 *
 *  Named from the target harness's catalog, and by its raw id for a harness whose option set is per
 *  agent (codex), which no catalog holds -- an id the user has not seen spelled that way, but the
 *  model they picked, which is the point. */
function convergingPickLabel(chat: ChatSnapshot): string | null {
  const converging = chat.handoff;
  const pick = converging?.model_pick ?? null;
  if (converging === null || converging.phase === "failed" || pick === null) return null;
  const options = getHarnessCatalog(converging.target_harness)?.options ?? [];
  const option = options.find((each) => each.id === pick.model_id);
  return modelPickLabel(option?.label ?? pick.model_id, pick.effort, pick.fast);
}

/** Past this many rows, a query rather than a scroll is the way to a model. */
const MODEL_SEARCH_CAP = 100;

/** The slider's filled portion, deepening with effort. It stops at 40% lightness: darker than
 *  that reads as near-black rather than as a deep green. */
function effortFillColor(fraction: number): string {
  return `hsl(152 39% ${Math.round(70 - 30 * fraction)}%)`;
}

/** The chip with no provider signed in: it says so, and a press opens the provider chooser. */
function notConnectedChip(onSignedIn?: (accountId: string) => void): m.Vnode {
  return m(
    "button",
    {
      type: "button",
      class: `model-selector-not-connected ${css.TRIGGER}`,
      ...hoverTooltipAttrs("Connect a model provider", "above"),
      onclick: () => openProviderChooser(onSignedIn === undefined ? {} : { onSignedIn }),
    },
    "Not connected",
  );
}

/** The chip while what the row would say has not arrived: it keeps the row from reading empty. */
function loadingChip(): m.Vnode {
  return m("span", { class: `model-selector-loading ${css.PROVISIONAL_CHIP}` }, "Loading…");
}

/** The chip for a chat with no agent yet: the account it starts on. With no agent there is no
 *  model to name or change. */
function provisionalChip(chatId: string): m.Vnode {
  const provisional = getProvisionalChat(chatId);
  if (provisional === undefined || !areAccountsLoaded()) return loadingChip();
  const account = accountForFirstSend(provisional.account_id);
  if (account === null) return notConnectedChip();
  return m(
    "span",
    {
      class: `model-selector-provisional ${css.PROVISIONAL_CHIP}`,
      ...hoverTooltipAttrs(`This chat starts on ${account.label}`, "above"),
    },
    account.provider,
  );
}

/** The chat's Source view switch, which the phone layout's menu carries as a row. */
export interface SourceViewSwitch {
  on: boolean;
  onToggle: () => void;
}

export interface ModelProviderMenuAttrs {
  chatId: string;
  /** The phone layout: the settings button and the sliding card, rather than the chip and its flyouts. */
  isCompact?: boolean;
  /** The Source view switch, when the chat has a terminal to turn over to; shown as a row in the phone layout. */
  sourceView?: SourceViewSwitch | null;
}

interface EffortControlOptions {
  efforts: readonly { level: string; in_picker: boolean }[];
  current: string | null;
  interactive: boolean;
  tooltip: string | null;
  onPick: (level: string) => void;
}

export function ModelProviderMenu(): m.Component<ModelProviderMenuAttrs> {
  let modelQuery = "";
  // The account-gated set of model ids to OFFER in a search picker, fetched fresh each
  // time the picker opens (so a login mid-session shows up). `null` means "offer the whole
  // catalog" -- the backend's answer for a static harness, or the not-yet-fetched state
  // (disambiguated by `offeredLoaded`). Only consulted for a searchable picker.
  let offeredModels: Set<string> | null = null;
  let offeredLoaded = false;
  let offeredLoading = false;
  // The FULL per-agent options for a DYNAMIC picker (codex), fetched fresh each time the picker
  // opens (D2 -- so a subscription-tier change shows up live). `null` until the first fetch (or when
  // the harness is not dynamic). Codex has no static catalog, so these ARE the picker's model rows.
  let dynamicOptions: CatalogModelOption[] | null = null;
  // Whether this open of the menu has already fetched its offerable models: with hover-opened
  // submenus a pointer crossing the Model row would otherwise re-run a `pi --list-models` that
  // takes up to 15s. Fresh per open is what matters -- a /login between two opens still shows
  // up -- so this resets with the menu.
  let offeredFetchedForOpen = false;
  // The account rows' own transient state -- an armed "Remove?", an open rename field.
  // Cleared whenever the submenu or the menu closes, so someone who clicked the bin to see
  // what it did does not come back later to a primed one.
  const rowState = emptyAccountRowState();
  // The index the pointer is currently dragging the effort slider to. Held locally because
  // mithril re-asserts `value` on every redraw, which would snap the thumb back under the
  // finger on a harness that does not move the chip optimistically.
  let draggingEffortIndex: number | null = null;
  // What the fast submenu's turn-limit field shows while it is being typed into, or null when it
  // shows the stored limit: mithril re-asserts `value` on every redraw, and every keystroke
  // causes one.
  let limitDraft: string | null = null;
  // What the last view saw, for the menu's own open hook to read: which chat is showing, and
  // whether its picker is the kind whose model list is worth warming.
  let viewedChatId = "";
  let viewedPickerIsFetched = false;
  let viewedIsCompact = false;

  /** What is scoped to a submenu: reset whenever the open submenu changes. */
  function resetSubmenuState(): void {
    rowState.confirmingRemoval = null;
    rowState.renamingId = null;
    rowState.renameDraft = "";
    limitDraft = null;
  }

  /** The open submenu changed, on the phone's track or the desktop's flyout. */
  function takeSubmenuChange(): void {
    resetSubmenuState();
    modelQuery = "";
  }

  const track = createSlidingMenuTrack({ onSubmenuChange: takeSubmenuChange });

  const menu = createMenu({
    // The menu hangs off the chip's top edge, because the composer sits at the bottom of the
    // panel and there is nothing under it to grow into.
    placement: "above",
    width: css.MENU_WIDTH,
    // A stable hook for tests, and for the composer's own styles.
    get extraClass() {
      return viewedIsCompact ? "model-provider-menu model-provider-menu--compact" : "model-provider-menu";
    },
    onOpen: () => {
      track.showSubmenu(null);
      modelQuery = "";
      offeredFetchedForOpen = false;
      // Warm the model list on the MENU's open rather than the submenu's: the fetch is the slow
      // part, and by the time a pointer has crossed the menu it is usually already back.
      if (viewedPickerIsFetched) warmOfferedModels(viewedChatId);
    },
    onClose: () => {
      // A drag that never released (the menu can be torn down mid-gesture) would otherwise
      // still be driving the label and the thumb the next time the menu opens.
      draggingEffortIndex = null;
      track.showSubmenu(null);
      resetSubmenuState();
    },
    onSubmenuChange: takeSubmenuChange,
    // A rename mid-type or an armed "Remove?" (providers), a typed search (model) and a
    // half-typed turn limit (fast) are all work a drifting pointer must not throw away.
    holdsSubmenuOpen: (key) => {
      if (key === "providers") {
        return rowState.renamingId !== null || rowState.confirmingRemoval !== null;
      }
      if (key === "fast") {
        return limitDraft !== null;
      }
      return modelQuery !== "";
    },
  });

  /** Back from a submenu to the menu's own rows, which stay up: the flyout closes, or the track slides back. */
  function closeSubmenu(): void {
    menu.closeSubmenu();
    track.showSubmenu(null);
  }

  // Recompute the offerable models for `chatId`. Called on every picker-open so a fresh
  // /login is reflected without reloading the page. A null `models` (offer everything) and
  // a fetch failure both leave `offeredModels` null -- the picker then shows the whole
  // catalog rather than an empty list.
  async function fetchOfferedModels(chatId: string): Promise<void> {
    offeredLoading = true;
    offeredLoaded = false;
    offeredModels = null;
    dynamicOptions = null;
    m.redraw();
    try {
      const response = await m.request<{ models: string[] | null; options?: CatalogModelOption[] | null }>({
        method: "GET",
        url: apiUrl("/api/chats/:chatId/model-options"),
        params: { chatId },
      });
      // A DYNAMIC harness (codex) answers with the full per-agent `options`; a static/gated harness
      // answers with `models` (ids), null meaning "offer the whole catalog".
      offeredModels = response.models == null ? null : new Set(response.models);
      dynamicOptions = response.options ?? null;
    } catch (error) {
      console.warn(`Failed to load offered models for chat ${chatId}`, error);
      offeredModels = null;
      dynamicOptions = null;
    } finally {
      offeredLoading = false;
      offeredLoaded = true;
      m.redraw();
    }
  }

  /** Load the chat's offerable models once per open. See `offeredFetchedForOpen`. */
  function warmOfferedModels(chatId: string): void {
    if (offeredFetchedForOpen) return;
    offeredFetchedForOpen = true;
    void fetchOfferedModels(chatId);
  }

  /** The effort slider, or null when there is nothing to slide.
   *
   * Two choices worth naming:
   *
   * 1. `onchange`, not `oninput`. The `input` event fires once per notch passed during a
   *    drag -- and every notch here is a live switch typed into the agent's pane (claude), a
   *    socket call (codex) or a parked intent (pi). `setModelChoice` chains rather than
   *    debounces, so a low-to-max drag would queue four sequential switches. This commits
   *    once, on release.
   * 2. Indexed over the SHOWN list, accepting that an agent on a hidden level (claude's
   *    `ultra`) pins its thumb at the far left. AT REST the label still reads correctly,
   *    because it comes from the value rather than the position. Mid-drag it follows the
   *    position instead, which is the point of dragging.
   */
  function effortRow(opts: EffortControlOptions): m.Children {
    const shown = opts.efforts.filter((effort) => effort.in_picker);
    // One stop is not a choice. pi's non-reasoning models declare exactly `("off",)`, and a
    // one-stop slider renders as an immovable full-green track labelled "Off" -- which looks
    // broken and says the opposite of the truth.
    if (shown.length < 2) return null;
    const committed = Math.max(
      0,
      shown.findIndex((effort) => effort.level === opts.current),
    );
    // Mid-drag the row reads off the thumb rather than off the committed choice, so the label
    // and the track fill travel with the finger instead of both sitting on the old level
    // until release. Only what is DRAWN moves; the commit is still once, on release.
    const position = draggingEffortIndex ?? committed;
    const pct = (position / (shown.length - 1)) * 100;
    // At rest the label comes from the value (which can name a level `shown` does not carry
    // -- see 2 above); mid-drag from the position, which indexes `shown` by construction
    // because the input's own min/max are its bounds.
    const level = draggingEffortIndex === null ? (opts.current ?? shown[committed].level) : shown[position].level;
    return m("div", { class: css.ROW_STATIC, ...hoverTooltipAttrs(opts.tooltip, "above") }, [
      m("span", { class: css.ROW_LABEL }, "Effort"),
      m("span", { class: css.ROW_VALUE_STATIC }, [
        m("span", { class: css.EFFORT_VALUE }, capitalizeEffort(level)),
        m("span", { class: css.SLIDER_WRAP }, [
          // A dot at each level EXCEPT the one the thumb is on, where the ball is the mark. It
          // is dropped from the list rather than hidden in place, because a keyed list may not
          // carry holes.
          //
          // A dot takes the colour of what it is drawn ON: the fill below the thumb, the bare
          // track above it.
          m(
            "span",
            { class: css.SLIDER_TICKS },
            shown
              .map((effort, index) => ({ effort, index }))
              .filter(({ index }) => index !== position)
              .map(({ effort, index }) =>
                m("span", {
                  key: effort.level,
                  class: index < position ? css.SLIDER_TICK_ON_FILL : css.SLIDER_TICK_ON_TRACK,
                  style: `left: ${(index / (shown.length - 1)) * 100}%`,
                }),
              ),
          ),
          m("input", {
            type: "range",
            "aria-label": "Reasoning effort",
            class: css.SLIDER,
            min: 0,
            max: shown.length - 1,
            step: 1,
            disabled: !opts.interactive,
            value: position,
            style:
              `background: linear-gradient(to right, ${effortFillColor(pct / 100)} ${pct}%, ` +
              `var(--color-fill-hover) ${pct}%)`,
            oninput: (event: Event) => {
              draggingEffortIndex = Number((event.target as HTMLInputElement).value);
            },
            onchange: (event: Event) => {
              const picked = shown[Number((event.target as HTMLInputElement).value)];
              draggingEffortIndex = null;
              if (picked !== undefined) opts.onPick(picked.level);
            },
          }),
        ]),
      ]),
    ]);
  }

  /** The effort in the phone layout: a segment per level rather than a slider, whose thumb is too small a target
   *  for a finger. A press commits the level at once, as a slider's release does. An agent on a level the picker does
   *  not offer lights no segment. */
  function effortSegments(opts: EffortControlOptions): m.Children {
    const shown = opts.efforts.filter((effort) => effort.in_picker);
    if (shown.length < 2) return null;
    return m("div", { class: css.EFFORT_SEGMENTS_ROW, ...hoverTooltipAttrs(opts.tooltip, "above") }, [
      m("span", { class: css.ROW_LABEL }, "Effort"),
      m(
        "div",
        { class: css.EFFORT_SEGMENTS, role: "radiogroup", "aria-label": "Reasoning effort" },
        shown.map((effort) => {
          const isCurrent = effort.level === opts.current;
          return m(
            "button",
            {
              type: "button",
              key: effort.level,
              role: "radio",
              "aria-checked": isCurrent ? "true" : "false",
              "data-effort-level": effort.level,
              class: isCurrent ? css.EFFORT_SEGMENT_ON : css.EFFORT_SEGMENT,
              disabled: !opts.interactive,
              onclick: () => {
                if (!isCurrent) opts.onPick(effort.level);
              },
            },
            capitalizeEffort(effort.level),
          );
        }),
      ),
    ]);
  }

  /** The Source view switch as a row of the phone layout's card. The whole row is the switch; turning it closes the
   *  card, whose chat face turns over with it. */
  function sourceViewRow(sourceView: SourceViewSwitch): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        role: "switch",
        "aria-checked": sourceView.on ? "true" : "false",
        class: menuRowClass({ extra: "text-primary" }),
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          menu.close();
          sourceView.onToggle();
        },
      },
      [
        m("span", { class: css.ROW_LABEL }, "Source view"),
        m(
          "span",
          { class: `ml-auto ${css.switchClass("sm")} ${sourceView.on ? css.SWITCH_ON : css.SWITCH_OFF}` },
          m("span", { class: css.switchKnobClass("sm", sourceView.on) }),
        ),
      ],
    );
  }

  /** A row that states a value and opens a DIALOG rather than a submenu, which is the one shape
   *  the shared menu has no kind for. Dressed as the submenu rows beside it: the shared row, its
   *  label a step back from its value, and a chevron saying there is somewhere to go. The menu
   *  closes on the way, because what opens is a modal over it. */
  function pickerRow(opts: { label: string; value: string; tooltip: string | null; onOpen: () => void }): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        class: menuRowClass({ extra: "text-primary" }),
        ...hoverTooltipAttrs(opts.tooltip, "above"),
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          menu.close();
          opts.onOpen();
        },
      },
      [
        m("span", { class: css.ROW_LABEL }, opts.label),
        m("span", { class: css.ROW_VALUE_STATIC }, [
          m("span", { class: "truncate" }, opts.value),
          m("span", { class: css.ROW_CHEVRON }, m.trust(icon("chevron-right", { size: 13 }))),
        ]),
      ],
    );
  }

  /** What the fast row reads, loading the chat's mode the first time it is asked for.
   *
   * The row states the MODE rather than a switch position, because auto is neither on nor off:
   * a chat in auto reads "Auto" while fast, "Auto (off now)" once its fast turns have run. */
  function fastModeValue(chatId: string): string {
    const state = getFastModeState(chatId);
    if (state === null) {
      void ensureFastModeState(chatId);
      return "...";
    }
    return fastModeLabel(state);
  }

  /** The Fast mode row's submenu: the chat's three modes, the limit auto runs to, and the mode
   *  new chats start in.
   *
   * Choosing applies at once (views/fast-mode-limit.ts), so there is nothing to confirm and the
   * submenu stays up: picking auto is usually followed by setting the limit it runs to. */
  function fastModeSubmenu(chatId: string): m.Children {
    const settings = getChatSettings();
    if (settings === null) void ensureChatSettings();
    const known = getFastModeState(chatId);
    if (known === null) void ensureFastModeState(chatId);
    const effective = settings ?? DEFAULT_CHAT_SETTINGS;
    const state: ChatFastModeState = known ?? { mode: effective.fast_mode_default, is_switched: false };
    const limit = effective.fast_mode_turn_limit;
    return [
      m(
        "div",
        { class: "fast-mode-options", role: "radiogroup", "aria-label": "Fast mode" },
        FAST_MODES.map((mode) => {
          const isCurrent = state.mode === mode;
          return m(
            "button",
            {
              type: "button",
              key: mode,
              role: "radio",
              "aria-checked": isCurrent ? "true" : "false",
              "data-fast-mode": mode,
              class: isCurrent ? css.FAST_ROW_SELECTED : css.FAST_ROW,
              onclick: () => {
                if (isCurrent) return;
                // Another mode takes the turn-limit field away, and a draft outliving it would
                // hold the submenu open for a field nobody can see. The field's own `onblur` does
                // not fire here: an element removed while focused never blurs, and on macOS a
                // press on a button does not move focus off it to begin with.
                limitDraft = null;
                chooseFastMode(chatId, mode, getEventsForChat(chatId));
              },
            },
            [
              m("span", { class: css.FAST_ROW_TEXT }, [
                m("span", { class: css.SUBMENU_ROW_NAME }, [
                  FAST_MODE_LABELS[mode],
                  // These rows are the bare mode names, so auto carries its own "(off now)":
                  // without it this row would read "Auto" while the row that opened the submenu,
                  // stating the same mode, reads "Auto (off now)".
                  isCurrent && mode === "auto" && state.is_switched
                    ? m("span", { class: "ml-1.5 type-helper text-faint" }, "(off now)")
                    : null,
                ]),
                m("span", { class: css.FAST_ROW_DETAIL }, fastModeDetail(mode, limit)),
              ]),
              isCurrent
                ? m("span", { class: css.SUBMENU_CHECK }, m.trust(icon("check", { size: 13, strokeWidth: 2.5 })))
                : null,
            ],
          );
        }),
      ),
      // The shared menu's own rule, role and all: this submenu's content is free-form, so it
      // borrows the chrome instead of getting a `divider` row.
      m("div", { role: "separator", class: menuDividerClass() }),
      state.mode === "auto"
        ? m("label", { class: css.FAST_LIMIT_ROW }, [
            "Turn off after",
            m(
              "span",
              { class: css.FAST_LIMIT_FIELD },
              m("input", {
                type: "number",
                min: 1,
                step: 1,
                class: inputClass({ extra: css.FAST_LIMIT_INPUT_EXTRA }),
                "aria-label": "Fast mode turn limit",
                value: limitDraft ?? String(limit),
                disabled: settings === null,
                oninput: (event: Event) => {
                  limitDraft = (event.target as HTMLInputElement).value;
                },
                onchange: (event: Event) => {
                  const typed = (event.target as HTMLInputElement).value;
                  limitDraft = null;
                  applyTurnLimit(typed);
                },
                onblur: () => {
                  limitDraft = null;
                },
                onkeydown: (event: KeyboardEvent) => {
                  if (event.key === "Enter") (event.target as HTMLInputElement).blur();
                },
              }),
            ),
            limit === 1 ? "turn" : "turns",
          ])
        : null,
      newChatsDefaultRow({
        markerClass: "fast-mode-default",
        groupLabel: "Fast mode for new chats",
        choices: FAST_MODES.map((mode) => ({
          value: mode,
          label: FAST_MODE_LABELS[mode],
          marker: { "data-fast-mode-default": mode },
        })),
        selected: settings?.fast_mode_default ?? null,
        onPick: (current, mode) => updateChatSettings({ ...current, fast_mode_default: mode }),
      }),
    ];
  }

  /** The row at the foot of a submenu naming the choice new chats start with: the workspace
   *  setting, as segments of its own, independent of the chat's choice above it. `selected` is
   *  the setting's value, null until the settings load, when no segment is lit and none can be
   *  pressed. `marker` is the data attribute each segment carries. */
  function newChatsDefaultRow<T extends string | boolean>(opts: {
    markerClass: string;
    groupLabel: string;
    choices: readonly { value: T; label: string; marker: Record<string, string> }[];
    selected: T | null;
    onPick: (current: ChatSettings, value: T) => Promise<unknown>;
  }): m.Vnode {
    return m("div", { class: `${opts.markerClass} ${css.DEFAULT_ROW}` }, [
      m("span", { class: css.ROW_LABEL }, "New chats start with"),
      m(
        "div",
        { class: css.DEFAULT_SEGMENTS, role: "radiogroup", "aria-label": opts.groupLabel },
        opts.choices.map((choice) => {
          const isSelected = opts.selected === choice.value;
          return m(
            "button",
            {
              type: "button",
              key: choice.label,
              role: "radio",
              "aria-checked": isSelected ? "true" : "false",
              ...choice.marker,
              class: isSelected ? css.DEFAULT_SEGMENT_ON : css.DEFAULT_SEGMENT,
              disabled: opts.selected === null,
              onclick: () => {
                const current = getChatSettings();
                if (current === null || isSelected) return;
                void opts.onPick(current, choice.value);
              },
            },
            choice.label,
          );
        }),
      ),
    ]);
  }

  /** What the Auto-compact row reads, loading the chat's setting the first time it is asked for. */
  function autocompactValue(chatId: string): string {
    const state = getAutocompactState(chatId);
    if (state === null) {
      void ensureAutocompactState(chatId);
      return "...";
    }
    return autocompactLabel(state.is_enabled);
  }

  /** The Auto-compact row's submenu: on or off for this chat, and what new chats start with. A
   *  choice applies at once and the submenu stays up. */
  function autocompactSubmenu(chatId: string): m.Children {
    const settings = getChatSettings();
    if (settings === null) void ensureChatSettings();
    const known = getAutocompactState(chatId);
    if (known === null) void ensureAutocompactState(chatId);
    const effective = settings ?? DEFAULT_CHAT_SETTINGS;
    const state: ChatAutocompactState = known ?? { is_enabled: effective.autocompact_default };
    return [
      m(
        "div",
        { class: "autocompact-options", role: "radiogroup", "aria-label": "Auto-compact" },
        [true, false].map((isEnabled) => {
          const isCurrent = state.is_enabled === isEnabled;
          return m(
            "button",
            {
              type: "button",
              key: String(isEnabled),
              role: "radio",
              "aria-checked": isCurrent ? "true" : "false",
              "data-autocompact": isEnabled ? "on" : "off",
              class: isCurrent ? css.SUBMENU_ROW_SELECTED : css.SUBMENU_ROW,
              onclick: () => {
                if (isCurrent) return;
                void setAutocompactState(chatId, { is_enabled: isEnabled });
              },
            },
            [
              m("span", { class: css.SUBMENU_ROW_NAME }, autocompactLabel(isEnabled)),
              isCurrent
                ? m("span", { class: css.SUBMENU_CHECK }, m.trust(icon("check", { size: 13, strokeWidth: 2.5 })))
                : null,
            ],
          );
        }),
      ),
      m(
        "p",
        { class: "autocompact-help px-3 py-1.5 type-helper text-faint" },
        "Saves ~50% by compacting right before the cache expires.",
      ),
      m("div", { role: "separator", class: menuDividerClass() }),
      newChatsDefaultRow({
        markerClass: "autocompact-default",
        groupLabel: "Auto-compact for new chats",
        choices: [true, false].map((isEnabled) => ({
          value: isEnabled,
          label: autocompactLabel(isEnabled),
          marker: { "data-autocompact-default": isEnabled ? "on" : "off" },
        })),
        selected: settings?.autocompact_default ?? null,
        onPick: (current, isEnabled) => updateChatSettings({ ...current, autocompact_default: isEnabled }),
      }),
    ];
  }

  /** File a changed turn limit on the workspace's settings. An emptied field, a word, or a
   *  number below one is not a limit, and leaves the stored one alone. */
  function applyTurnLimit(typed: string): void {
    const current = getChatSettings();
    if (current === null) return;
    const parsed = Number.parseInt(typed, 10);
    if (Number.isNaN(parsed) || parsed < 1 || parsed === current.fast_mode_turn_limit) return;
    void updateChatSettings({ ...current, fast_mode_turn_limit: parsed });
  }

  /** The chat's reversible process verb: ``mngr stop`` on the agent, which a later message or
   *  start brings back. No confirmation -- it is one message away from undone. The agent list
   *  catches up through the observe stream. */
  function stopAgent(targetChatId: string): void {
    void fetch(apiUrl(`/api/chats/${encodeURIComponent(targetChatId)}/stop`), { method: "POST" })
      .then(async (response) => {
        if (response.ok) return;
        const data = (await response.json().catch(() => ({}))) as { detail?: string };
        alert(`Failed to stop the agent: ${data.detail ?? `HTTP ${response.status}`}`);
      })
      .catch((e: Error) => {
        alert(`Failed to stop the agent: ${e.message}`);
      });
  }

  /** A row of the submenu that is still fetching its contents. */
  function loadingRow(): m.Vnode {
    return m("div", { class: `${css.SUBMENU_EMPTY} flex items-center gap-2` }, [
      m("span", { class: "pv-spinner" }),
      "Loading models...",
    ]);
  }

  /** The Provider row's submenu: every signed-in account, plus a way to add one.
   *
   * Pressing any account but the chat's own begins the switch to it (``beginSwitchTo``): the
   * dialog for a handoff, which takes the model and offers a new chat instead; armed at once for
   * a rebind (an account on the chat's own harness and lane) and for a chat whose account was
   * signed out; run at once for a chat with no user turn yet. Pressing the armed account again, or
   * the account the chat runs on, takes the choice back. Each row also carries the default toggle:
   * the starred account is the one a new chat opens on when nothing names one (the New Tab tile,
   * the rail shortcut, an agent's `layout.py open chat`).
   */
  function providerSubmenu(chatId: string, current: ProviderAccount | null): m.Children {
    const rows = getAccounts();
    const defaultId = getDefaultAccountId();
    const nextId = nextSendSwitchTarget(chatId)?.id ?? null;
    const chat = getChatById(chatId);
    return [
      // Built as one list rather than with a conditional hole beside it: mithril refuses a
      // fragment that mixes keyed vnodes with a null, and every row here is keyed.
      m(
        "div",
        { class: css.SUBMENU_SCROLL },
        rows.length === 0
          ? [m("div", { class: css.SUBMENU_EMPTY }, "No providers yet.")]
          : rows.map((row) => {
              const isCurrent = current !== null && row.id === current.id;
              const isNext = row.id === nextId;
              return accountRow({
                row,
                isCurrent,
                isDefault: row.id === defaultId,
                rowClass: isCurrent ? css.ACCOUNT_ROW_SELECTED : css.ACCOUNT_ROW,
                ...(isNext ? { badge: "next" } : {}),
                onSelect: () => {
                  if (isCurrent || isNext || chat === undefined || !isSwitchTarget(chat, row)) {
                    closeSubmenu();
                    takeBackSwitch(chatId);
                    return;
                  }
                  menu.close();
                  beginSwitchTo(chatId, row);
                },
                state: rowState,
              });
            }),
      ),
      m(
        "button",
        {
          type: "button",
          class: css.SUBMENU_ADD,
          onclick: () => {
            menu.close();
            openProviderChooser({
              ...(current !== null ? { unpickable: { accountId: current.id, reason: "current" as const } } : {}),
              onSignedIn: (accountId) => {
                // Signed in from inside a chat: the new account is what the user switches this
                // chat to next, so the switch begins on it.
                beginSwitchToAccountId(chatId, accountId);
                m.redraw();
              },
            });
          },
        },
        "+ Add a provider",
      ),
    ];
  }

  /** The Model row's submenu: the models this account can actually use. */
  function modelSubmenu(
    chatId: string,
    sourceOptions: readonly CatalogModelOption[],
    matched: CatalogModelOption | null,
    currentIdentity: ModelIdentity,
    optimistic: boolean,
    searchable: boolean,
    dynamic: boolean,
  ): m.Children {
    const offeredIds = searchable && offeredLoaded ? offeredModels : null;
    const all = sourceOptions
      .filter((option) => option.in_picker)
      .filter((option) => offeredIds === null || offeredIds.has(option.id));
    const query = modelQuery.trim().toLowerCase();
    const filtered = query === "" ? all : all.filter((option) => option.label.toLowerCase().includes(query));
    const visible = filtered.slice(0, MODEL_SEARCH_CAP);
    const loading = (searchable || dynamic) && (offeredLoading || !offeredLoaded);
    const hasSearchField = searchable || all.length > 8;
    return [
      // ABOVE the list: the field is where the pointer arrives and where the typing starts, and
      // it stays put while the list scrolls beneath it.
      hasSearchField
        ? m("div", { class: css.SEARCH_WRAP }, [
            m("span", { class: css.SEARCH_ICON }, m.trust(icon("search", { size: 13 }))),
            m("input", {
              class: inputClass({ extra: css.SEARCH_INPUT_EXTRA }),
              type: "text",
              placeholder: "Search models",
              value: modelQuery,
              oncreate: (inputVnode: m.VnodeDOM) => (inputVnode.dom as HTMLInputElement).focus(),
              oninput: (event: Event) => {
                modelQuery = (event.target as HTMLInputElement).value;
              },
            }),
          ])
        : null,
      // One list or the other, never a hole beside keyed rows -- mithril refuses a fragment
      // that mixes the two, and it throws during the DOM diff rather than at build time.
      m(
        "div",
        { class: css.SUBMENU_SCROLL },
        loading
          ? [loadingRow()]
          : visible.length === 0
            ? [m("div", { class: css.SUBMENU_EMPTY }, "No models available.")]
            : visible.map((option) => {
                const isCurrent = matched !== null && option.id === matched.id;
                return m(
                  "button",
                  {
                    type: "button",
                    key: option.id,
                    class: isCurrent ? css.SUBMENU_ROW_SELECTED : css.SUBMENU_ROW,
                    onclick: () => {
                      const next: ModelIdentity = {
                        model_id: option.id,
                        effort: clampEffort(option, effortInEffect(currentIdentity.effort, matched)),
                        fast: option.supports_fast ? currentIdentity.fast : false,
                      };
                      setModelChoice(chatId, next, option, changedAxes(currentIdentity, next), optimistic);
                      // The menu stays, with the new model's effort and fast rows there to adjust.
                      closeSubmenu();
                    },
                  },
                  [
                    // Front-truncated: an openrouter id is a path whose tail tells rows apart.
                    startTruncated(option.label),
                    isCurrent
                      ? m("span", { class: css.SUBMENU_CHECK }, m.trust(icon("check", { size: 13, strokeWidth: 2.5 })))
                      : null,
                  ],
                );
              }),
      ),
    ];
  }

  return {
    oninit() {
      // The catalogs are static and shared; load them once.
      void ensureHarnessCatalogs();
    },

    onremove() {
      menu.dispose();
    },

    view(vnode) {
      const { chatId } = vnode.attrs;
      const isCompact = vnode.attrs.isCompact === true;
      const sourceView = vnode.attrs.sourceView ?? null;
      viewedIsCompact = isCompact;
      const chat = getChatById(chatId);
      const account = accountForAgent(chat?.active_agent.account_id ?? undefined);
      const catalog: HarnessCatalog | null = getHarnessCatalog(chat?.active_agent.harness);
      const choice = catalog === null ? null : effectiveChoice(chatId, chat?.active_agent.model_choice);
      const matched = choice?.matched ?? null;

      // THREE states have no model, not one, and the Provider row must render in all of them:
      // the provider is a property of the ACCOUNT, not of the model. The catalog may not have
      // loaded; the live choice may not have resolved (every harness passes through this
      // before its first model read, and opencode never leaves it); or the live model may
      // match no catalog option. Only the Model/Effort/Fast rows are suppressed.
      if (chat === undefined) return provisionalChip(chatId);
      // A chat whose account was signed out runs on nothing until a provider is chosen for it, and
      // the composer's notice in its place says so and offers the choice: the model it last ran
      // stays out of the row rather than reading as current. A phone keeps its settings button.
      if (
        !isCompact &&
        chat.handoff === null &&
        pendingSwitchTarget(chatId) === null &&
        isAccountSignedOut(chat.active_agent.account_id)
      ) {
        return null;
      }
      // No account to name and no model to show. With no provider signed in, say so; otherwise
      // the chip below stands in (its Provider row reads "No account") until the model arrives,
      // which for a chat whose agent is still connecting takes a while. A phone keeps its settings
      // button, which is also its way to Source view and Stop agent.
      if (account === null && matched === null && !isCompact) {
        if (!areAccountsLoaded()) return loadingChip();
        if (getAccounts().length === 0) {
          return notConnectedChip((accountId) => {
            beginSwitchToAccountId(chatId, accountId);
            m.redraw();
          });
        }
      }

      // opencode ships an empty catalog and a resolver that fails, so its every pick would
      // 500. Read-only is the honest render -- a picker there offers a switch that cannot work.
      const readOnly = catalog === null || catalog.switch_mode === "read_only";
      const interactive = !readOnly && matched !== null;
      const optimistic = catalog?.switch_mode === "eager_then_reconcile";
      // What the agent recorded, which a switch diffs against; null while it runs at the default.
      const recordedEffort = choice?.identity.effort ?? null;
      // What every effort display shows.
      const currentEffort = effortInEffect(recordedEffort, matched);
      const currentFast = choice?.identity.fast ?? false;
      const shownEfforts = (matched?.efforts ?? []).filter((effort) => effort.in_picker);
      const readOnlyTooltip = interactive ? null : READ_ONLY_TOOLTIP;
      const searchable = catalog?.picker_mode === "search";
      const dynamic = catalog?.picker_mode === "dynamic";
      viewedChatId = chatId;
      viewedPickerIsFetched = searchable || dynamic;

      // The account the chat is moving to, and the model it runs on there: while a switch is armed
      // for the next send, or being carried out, the menu reads as the target, since that is what
      // the chat's messages run on. Only an armed one is marked "next". With nothing picked, a
      // rebind keeps the agent's model and a handoff's successor starts on its harness's default,
      // which the menu has no name for until the harness reports it.
      const armed = nextSendSwitchTarget(chatId);
      const pending = armed ?? underwaySwitchTarget(chatId);
      const pendingPick = getPendingPick(chatId);
      const isPendingRebind = pending !== null && switchKind(chat, pending) === "rebind";
      const pendingModelLabel = pendingPick?.label ?? (isPendingRebind ? (matched?.label ?? null) : null);
      const pendingModelValue = pendingModelLabel ?? (isPendingRebind ? "Current model" : "Default model");
      // A page with no switch of its own can still be watching one: reloaded mid-switch, it has
      // only what the chat carries. Read the same way, so the chip does not fall back to a live
      // choice that cannot name the picked model until the harness has taken it.
      const convergingLabel = pending !== null ? null : convergingPickLabel(chat);

      // The chip states the WHOLE choice, from the same three values the menu's rows read --
      // one source, so the summary and the detail cannot disagree. Effort appears only when
      // the model has one to state, and the bolt only when fast is actually on. A switch replaces
      // all of it with the target's pick, marked "next" while the next send is what carries it out.
      const trigger = m(
        "button",
        {
          type: "button",
          // A stable hook for the composer's own styles and for tests.
          class: `model-selector-trigger ${css.TRIGGER}`,
          ...menu.triggerAttrs(),
          ...hoverTooltipAttrs("Change model or provider", "above"),
        },
        pending !== null || convergingLabel !== null
          ? [
              m("span", convergingLabel ?? pendingModelLabel ?? pending?.harness_label ?? ""),
              armed !== null
                ? m("span", { class: `model-provider-menu-next-badge ${css.NEXT_BADGE} ml-1.5` }, "next")
                : null,
            ]
          : [
              // A dot joins the text parts; the bolt stands on the row's gap alone, since a glyph
              // is already read apart from the words.
              m("span", matched?.label ?? account?.provider ?? "Model"),
              shownEfforts.length > 1 && currentEffort !== null
                ? [m("span", { class: css.TRIGGER_DOT }, "·"), m("span", capitalizeEffort(currentEffort))]
                : null,
              currentFast
                ? m("span", { class: "flex items-center" }, m.trust(icon("zap", { size: 12, filled: true })))
                : null,
            ],
      );

      // The phone layout's way into the same card: a settings button at the composer's left, since the chip's words
      // do not fit beside a phone's composer.
      const opener = isCompact
        ? m(
            "button",
            {
              type: "button",
              class: `composer-settings-button ${css.SETTINGS_BUTTON}`,
              "data-composer-settings": "",
              "aria-label": "Chat settings",
              ...menu.triggerAttrs(),
            },
            slidersGlyph(),
          )
        : trigger;

      if (!menu.isOpen()) return opener;

      const currentIdentity: ModelIdentity =
        matched === null
          ? { model_id: "", effort: recordedEffort, fast: currentFast }
          : { model_id: matched.id, effort: recordedEffort, fast: currentFast };
      const sourceOptions: CatalogModelOption[] = dynamic ? (dynamicOptions ?? []) : (catalog?.options ?? []);

      const rows: MenuRow[] = [];
      if (armed !== null) {
        // The target's own rows: the account the next send moves the chat to and the model it
        // runs on there, whose row opens the dialog again to change it.
        rows.push({
          kind: "submenu",
          key: "providers",
          label: "Provider",
          value: armed.provider,
          sub: `${armed.harness_label}, next message`,
          content: () => providerSubmenu(chatId, account),
        });
        rows.push({ kind: "divider" });
        rows.push({
          kind: "custom",
          key: "model",
          render: () =>
            pickerRow({
              label: "Model",
              value: pendingModelValue,
              tooltip: "Change the model this chat switches to",
              onOpen: () => openSwitchDialog(chatId, armed),
            }),
        });
      } else if (pending !== null) {
        // The same rows while the switch is carried out, as values: the model it carries is fixed
        // once it is sent.
        rows.push({
          kind: "value",
          key: "providers",
          label: "Provider",
          value: pending.provider,
          sub: `${pending.harness_label}, switching`,
        });
        rows.push({ kind: "divider" });
        rows.push({
          kind: "value",
          key: "model",
          label: "Model",
          value: pendingModelValue,
          truncateValue: "start",
        });
      } else {
        rows.push({
          kind: "submenu",
          key: "providers",
          label: "Provider",
          value: account?.provider ?? "No account",
          sub: account?.harness_label ?? "Pick one to move this chat to it",
          content: () => providerSubmenu(chatId, account),
        });
        rows.push({ kind: "divider" });
        if (matched !== null) {
          // A read-only harness gets a row that states the model and nothing more: no chevron,
          // no list.
          rows.push(
            interactive
              ? {
                  kind: "submenu",
                  key: "model",
                  label: "Model",
                  value: matched.label,
                  truncateValue: "start",
                  maxHeight: css.MODEL_SUBMENU_MAX_HEIGHT,
                  onOpen: () => {
                    if (searchable || dynamic) warmOfferedModels(chatId);
                  },
                  content: () =>
                    modelSubmenu(chatId, sourceOptions, matched, currentIdentity, optimistic, searchable, dynamic),
                }
              : {
                  kind: "value",
                  key: "model",
                  label: "Model",
                  value: matched.label,
                  truncateValue: "start",
                  tooltip: readOnlyTooltip ?? undefined,
                },
          );
          rows.push({
            kind: "custom",
            key: "effort",
            render: () =>
              (isCompact ? effortSegments : effortRow)({
                efforts: matched.efforts,
                current: currentEffort,
                interactive,
                tooltip: readOnlyTooltip,
                onPick: (level) => {
                  const next: ModelIdentity = { model_id: matched.id, effort: level, fast: currentFast };
                  setModelChoice(chatId, next, matched, changedAxes(currentIdentity, next), optimistic);
                },
              }),
          });
          if (matched.supports_fast) {
            rows.push(
              interactive
                ? {
                    kind: "submenu",
                    key: "fast",
                    label: "Fast mode",
                    value: fastModeValue(chatId),
                    content: () => fastModeSubmenu(chatId),
                  }
                : {
                    kind: "value",
                    key: "fast",
                    label: "Fast mode",
                    value: fastModeValue(chatId),
                    tooltip: readOnlyTooltip ?? undefined,
                  },
            );
          }
        }
        if (catalog?.supports_compaction === true) {
          rows.push(
            readOnly
              ? { kind: "value", key: "autocompact", label: "Auto-compact", value: autocompactValue(chatId) }
              : {
                  kind: "submenu",
                  key: "autocompact",
                  label: "Auto-compact",
                  value: autocompactValue(chatId),
                  content: () => autocompactSubmenu(chatId),
                },
          );
        }
      }
      if (isCompact && sourceView !== null) {
        rows.push({ kind: "divider" });
        rows.push({ kind: "custom", key: "source-view", render: () => sourceViewRow(sourceView) });
      }
      rows.push({ kind: "divider" });
      rows.push({ kind: "action", key: "stop-agent", label: "Stop agent", onSelect: () => stopAgent(chatId) });

      if (!isCompact) return [trigger, menu.view(rows)];
      return [
        opener,
        menu.view([{ kind: "custom", key: "sliding-track", render: () => track.view(rows, () => menu.close()) }]),
      ];
    },
  };
}
