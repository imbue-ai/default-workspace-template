/**
 * The composer's combo card: which PROVIDER this chat runs on, and which model on it.
 *
 * The provider leads, because with several accounts signed in it is the first thing worth
 * knowing about a chat.
 *
 * Everything it shows is data: the static per-harness catalog from HarnessCatalog.ts, the
 * agent's live `model_choice` pushed onto the agents store, and its `account` label resolved
 * against the account list. Which rows show is decided by the matched catalog option (effort
 * iff the model declares more than one; fast iff it supports it); the switch mode decides
 * whether they are interactive.
 *
 * The provider row is the one that always renders. A provider is a property of the ACCOUNT,
 * not of the model, so it survives all three of the states in which there is no model to show.
 *
 * The card opens on a CLICK of the chip and its side flyouts open on HOVER, which is the
 * division every desktop menu makes: getting into a menu is a decision, moving around inside
 * one is not. Inside the card the pointer therefore decides what is showing -- every row takes
 * the hover, including the rows that open nothing and so close what is open -- and the safe
 * triangle (`isInSafeTriangle`) is what keeps the rows lying between the pointer and an open
 * flyout from stealing it on the way across. Outside the card nothing changes: only a click
 * takes the stack down.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { getAgentById } from "../models/AgentManager";
import type { CatalogModelOption, HarnessCatalog } from "../models/HarnessCatalog";
import { ensureHarnessCatalogs, getHarnessCatalog } from "../models/HarnessCatalog";
import { changedAxes, effectiveChoice, setModelChoice } from "../models/ModelSettings";
import type { ModelIdentity } from "../models/ModelSettings";
import { accountForAgent, getAccounts, getDefaultAccountId, openProviderChooser } from "../models/Providers";
import type { ProviderAccount } from "../models/Providers";
import { startChatOnAccount } from "../shell";
import { isInSafeTriangle, placeFlyout } from "@imbue/workspace-ui/src/flyout-position";
import type { FlyoutPoint } from "@imbue/workspace-ui/src/flyout-position";
import { Portal } from "@imbue/workspace-ui/src/portal";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import { accountRow, emptyAccountRowState } from "./accountRow";
import * as css from "./modelCardStyles";

/** Shown on a read-only harness's rows. agy's `/model` is an interactive TUI with no
 *  scriptable form, so the card cannot drive it -- and says where the user can. */
const READ_ONLY_TOOLTIP = "To change the model or effort, run /model or /effort in the agent terminal.";

/** The effort to carry when switching to `option`: keep the current one if the new
 *  model declares it, else the model's first shown (or first declared) effort. Null
 *  when the model has no effort axis. */
function clampEffort(option: CatalogModelOption, currentEffort: string | null): string | null {
  if (option.efforts.length === 0) {
    return null;
  }
  if (currentEffort !== null && option.efforts.some((effort) => effort.level === currentEffort)) {
    return currentEffort;
  }
  const shown = option.efforts.filter((effort) => effort.in_picker);
  return (shown[0] ?? option.efforts[0]).level;
}

function capitalizeEffort(level: string): string {
  return level.length === 0 ? level : level[0].toUpperCase() + level.slice(1);
}

// A search picker (pi) can carry thousands of models; never lay out more than
// this many <li> at once. The user narrows with the search box; the cap bounds the DOM.
const MODEL_SEARCH_CAP = 100;

/** Gap kept between the card (and its flyout) and every viewport edge. */
const CARD_MARGIN = 8;

/** Marks the trigger, the card and the flyout as one popover stack, so an outside-click test
 *  is a `closest` call rather than three element references that can go stale. */
const POPOVER_ATTR = "data-model-popover";

/** How long the pointer rests on a row before that row's flyout opens.
 *
 *  Shorter than the tooltip's 250ms on purpose: a tooltip is an aside nobody asked for, so it
 *  waits until it is clearly wanted, while a submenu IS what the pointer came across the card
 *  for. Long enough that a sweep to a row further along opens nothing on the way. */
const SUBMENU_HOVER_DELAY_MS = 150;

/** How long the safe triangle survives once the pointer is inside it.
 *
 *  Crossing the card is a flick and is over well inside this. A pointer still in the wedge
 *  after it has stopped to read the row it is parked on, so the wedge lets go and that row
 *  gets to open its own flyout -- otherwise a row reached diagonally could never be opened by
 *  hover at all. */
const SAFE_TRIANGLE_GRACE_MS = 400;

/** The slider's filled portion, deepening with effort. */
function effortFillColor(fraction: number): string {
  return `hsl(152 39% ${Math.round(70 - 40 * fraction)}%)`;
}

export function ModelBar(): m.Component<{ agentId: string }> {
  // The current model-search query (only used when the harness's picker_mode is "search").
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

  // Where the card and its flyout sit, captured when each opens. Both are `fixed` and
  // portalled out of the composer, so they carry viewport coordinates rather than being
  // laid out by their parent -- see `openCard`.
  let cardAnchor: DOMRect | null = null;
  let flyout: "model" | "providers" | null = null;
  let flyoutRowBottom = 0;
  // The open flyout's measured box, remeasured on every redraw it survives, because its
  // height follows its content (a filtered model list is shorter). It is the safe triangle's
  // base, so a stale one would protect the wrong wedge.
  let flyoutRect: DOMRect | null = null;
  // The safe triangle's apex: the last point the pointer occupied on the row that opened the
  // flyout, i.e. where it set off from. Null when there is nothing to travel to, or once the
  // pointer has arrived -- or once it has spent `SAFE_TRIANGLE_GRACE_MS` in the wedge without
  // arriving, which the timer beside it decides.
  let safeApex: FlyoutPoint | null = null;
  let safeApexTimer: number | null = null;
  // The row a hover is waiting on, held as the ELEMENT rather than as which-flyout-it-opens so
  // that "already waiting on this row" is one identity test -- the rows that open nothing all
  // answer `null` to that question and would collide.
  let hoverIntentRow: HTMLElement | null = null;
  let hoverIntentTimer: number | null = null;
  // Whether this card has already fetched its offerable models. The card's own open warms them
  // (see the trigger), and with hover-opened flyouts a pointer crossing the Model row would
  // otherwise re-run a `pi --list-models` that takes up to 15s. Fresh per card-open is what
  // matters -- a /login between two opens still shows up -- so this resets with the card.
  let offeredFetchedForCard = false;
  // The provider rows' own transient state -- an armed "Remove?", an open rename field.
  // Cleared whenever the flyout or the card closes, so someone who clicked the bin to see
  // what it did does not come back later to a primed one.
  const rowState = emptyAccountRowState();
  // The account whose row was pressed and is waiting for "Launch" or "Cancel". A chat's
  // account is fixed at create time, so pressing another account's row can only mean a new
  // chat on it -- asked, not done by surprise. Cleared with the rest of the flyout's state.
  let launchPromptAccountId: string | null = null;
  const launchDialog = makeNoticeDialog();
  // The index the pointer is currently dragging the effort slider to. Held locally because
  // mithril re-asserts `value` on every redraw, which would snap the thumb back under the
  // finger on a harness that does not move the chip optimistically.
  let draggingEffortIndex: number | null = null;

  // Recompute the offerable models for `agentId`. Called on every picker-open so a fresh
  // /login is reflected without reloading the page. A null `models` (offer everything) and
  // a fetch failure both leave `offeredModels` null -- the picker then shows the whole
  // catalog rather than an empty list.
  async function fetchOfferedModels(agentId: string): Promise<void> {
    offeredLoading = true;
    offeredLoaded = false;
    offeredModels = null;
    dynamicOptions = null;
    m.redraw();
    try {
      const response = await m.request<{ models: string[] | null; options?: CatalogModelOption[] | null }>({
        method: "GET",
        url: apiUrl("/api/agents/:agentId/model-options"),
        params: { agentId },
      });
      // A DYNAMIC harness (codex) answers with the full per-agent `options`; a static/gated harness
      // answers with `models` (ids), null meaning "offer the whole catalog".
      offeredModels = response.models == null ? null : new Set(response.models);
      dynamicOptions = response.options ?? null;
    } catch (error) {
      console.warn(`Failed to load offered models for agent ${agentId}`, error);
      offeredModels = null;
      dynamicOptions = null;
    } finally {
      offeredLoading = false;
      offeredLoaded = true;
      m.redraw();
    }
  }

  /** Load this agent's offerable models once per card-open. See `offeredFetchedForCard`. */
  function warmOfferedModels(agentId: string): void {
    if (offeredFetchedForCard) return;
    offeredFetchedForCard = true;
    void fetchOfferedModels(agentId);
  }

  function openCard(trigger: HTMLElement): void {
    cardAnchor = trigger.getBoundingClientRect();
    setFlyout(null);
    modelQuery = "";
    offeredFetchedForCard = false;
  }

  function closeCard(): void {
    cardAnchor = null;
    // A drag that never released (the card can be torn down mid-gesture) would otherwise
    // still be driving the label and the thumb the next time the card opens.
    draggingEffortIndex = null;
    cancelHoverIntent();
    setFlyout(null);
  }

  /** The ONLY way the open flyout changes.
   *
   *  An armed "Remove?" belongs to the submenu it was armed in and must outlive everything
   *  short of that submenu going away -- another row being pressed, the pointer wandering off,
   *  a redraw. Routing every change through here is what makes "until the submenu closes"
   *  true by construction rather than by remembering to clear it at four call sites. */
  function setFlyout(next: "model" | "providers" | null): void {
    if (next !== flyout) {
      rowState.confirmingRemoval = null;
      rowState.renamingId = null;
      rowState.renameDraft = "";
      launchPromptAccountId = null;
      // A new flyout is a new box in a new place: nothing has travelled towards it yet, and
      // the old one's measurements describe a box that is gone.
      clearSafeApex();
      flyoutRect = null;
    }
    flyout = next;
  }

  /** Drop a hover that has not opened anything yet. */
  function cancelHoverIntent(): void {
    if (hoverIntentTimer !== null) {
      window.clearTimeout(hoverIntentTimer);
      hoverIntentTimer = null;
    }
    hoverIntentRow = null;
  }

  /** Forget the trip: no wedge, and no clock counting one down. */
  function clearSafeApex(): void {
    if (safeApexTimer !== null) {
      window.clearTimeout(safeApexTimer);
      safeApexTimer = null;
    }
    safeApex = null;
  }

  /** Whether the pointer is on its way to the flyout that is already open, rather than
   *  changing its mind about which row it wants.
   *
   *  Only ever true while a flyout is up AND the pointer has been on the row that opened it,
   *  so the wedge cannot outlive the trip it was measured for. */
  function isTravellingToFlyout(point: FlyoutPoint): boolean {
    if (flyout === null || safeApex === null || flyoutRect === null || cardAnchor === null) return false;
    // Which of the flyout's edges faces the card -- `placeFlyout` puts it on either side. Read
    // off the two boxes' centers rather than off the near edges, which overlap by design.
    const trailing = flyoutRect.left + flyoutRect.width / 2 > cardLeft(cardAnchor) + css.CARD_WIDTH / 2;
    return isInSafeTriangle(point, safeApex, {
      edgeX: trailing ? flyoutRect.left : flyoutRect.right,
      top: flyoutRect.top,
      bottom: flyoutRect.bottom,
    });
  }

  /** Show `next` (or nothing) because the pointer or the keyboard settled on its row. */
  function openFlyoutFromRow(next: "model" | "providers" | null, row: HTMLElement, onOpen?: () => void): void {
    if (next === flyout) return;
    if (next !== null) {
      flyoutRowBottom = row.getBoundingClientRect().bottom;
    }
    setFlyout(next);
    if (next !== null) {
      modelQuery = "";
      onOpen?.();
    }
  }

  /** Every card row's pointer and keyboard handling, in one recipe.
   *
   *  `which` is the flyout the row opens, or null for a row that opens nothing -- and a row
   *  that opens nothing still takes the hover, closing whatever is open. Within the card the
   *  pointer decides what is showing; only outside it does a click still have to.
   *
   *  `mousemove` rather than `mouseenter`, for two reasons. It keeps the safe triangle's apex
   *  on the pointer's actual last position over the owning row instead of on wherever it first
   *  crossed the edge. And a row entered THROUGH the triangle -- protected, so it opened
   *  nothing -- gets another chance as soon as the pointer moves off the wedge, which a single
   *  enter event cannot give it. */
  function hoverRowAttrs(opts: { which: "model" | "providers" | null; onOpen?: () => void }): m.Attributes {
    return {
      onmousemove: (event: MouseEvent) => {
        const row = event.currentTarget as HTMLElement;
        const point: FlyoutPoint = { x: event.clientX, y: event.clientY };
        // On the row whose flyout is up: this is the trip's starting point, right up until the
        // pointer leaves. Every move here restarts it, so the apex is the true exit point and
        // the grace clock only ever runs on a trip that has actually set off.
        if (opts.which !== null && opts.which === flyout) {
          clearSafeApex();
          safeApex = point;
          cancelHoverIntent();
          return;
        }
        if (opts.which === null && flyout === null) return;
        if (isTravellingToFlyout(point)) {
          // Started across. Arriving cancels this (the flyout's own `mouseenter`); parking in
          // the wedge instead lets it run out, and the row underneath gets its turn.
          if (safeApexTimer === null) {
            safeApexTimer = window.setTimeout(() => {
              safeApexTimer = null;
              safeApex = null;
            }, SAFE_TRIANGLE_GRACE_MS);
          }
          return;
        }
        // Already counting down on this very row; restarting the clock would mean a pointer
        // that keeps twitching never opens anything.
        if (hoverIntentRow === row) return;
        cancelHoverIntent();
        hoverIntentRow = row;
        hoverIntentTimer = window.setTimeout(() => {
          hoverIntentTimer = null;
          hoverIntentRow = null;
          openFlyoutFromRow(opts.which, row, opts.onOpen);
          m.redraw();
        }, SUBMENU_HOVER_DELAY_MS);
      },
      // Keyboard focus opens immediately -- there is no aiming to wait for, and a tab that has
      // landed on a row is as deliberate as an intent delay could ever prove. `focusin`, which
      // bubbles, so a row whose focusable part is a child (the effort slider) is covered too.
      // `:focus-visible` keeps a mouse click out of this path: it focuses the row as well, and
      // opening from here would race the click's own toggle.
      onfocusin: (event: FocusEvent) => {
        const focused = event.target as HTMLElement | null;
        if (focused === null || !focused.matches(":focus-visible")) return;
        cancelHoverIntent();
        openFlyoutFromRow(opts.which, event.currentTarget as HTMLElement, opts.onOpen);
      },
    };
  }

  /** A click outside the card, its flyout and its trigger closes the whole stack -- and only
   *  a click does; a pointer that merely drifts off leaves everything up.
   *
   *  The test is `closest`, not a cached element reference. References captured in `oncreate`
   *  go stale or arrive late, and when they do this handler decides an inside click was an
   *  outside one and tears the popover down on mousedown -- before the click that was supposed
   *  to act ever reaches its button. That is what made the trash and "+ Add a provider" look
   *  like they did nothing. The DOM already knows the answer; ask it. */
  function handleOutsideMousedown(event: MouseEvent): void {
    if (cardAnchor === null) return;
    const target = event.target as Element | null;
    if (target?.closest?.(`[${POPOVER_ATTR}]`) != null) return;
    closeCard();
    m.redraw();
  }

  /** A row's tooltip attrs, or nothing when it has none to give. Spread, not wrapped: the
   *  bubble lives on <body>, so the row needs no container of its own. */
  function tooltipAttrs(text: string | null): m.Attributes {
    return text === null ? {} : hoverTooltipAttrs(text);
  }

  /** One card row that opens a flyout, or -- when `openable` is false -- one that just states
   *  its value and explains, on hover, where it can be changed instead. */
  function menuRow(opts: {
    label: string;
    value: string;
    sub?: string;
    which: "model" | "providers";
    openable: boolean;
    tooltip: string | null;
    onOpen?: () => void;
  }): m.Vnode {
    const row = m(
      "button",
      {
        type: "button",
        class: opts.openable ? css.ROW : css.ROW_INERT,
        // A stable hook so a test can address a row by what it is rather than by its classes.
        "data-card-row": opts.which,
        // The row is a disclosure, and a hover menu has to say so out loud: the chevron is the
        // only other clue, and it is decoration to a screen reader.
        "aria-haspopup": opts.openable ? "true" : undefined,
        "aria-expanded": opts.openable ? (flyout === opts.which ? "true" : "false") : undefined,
        ...tooltipAttrs(opts.tooltip),
        // HOVER opens these, after `SUBMENU_HOVER_DELAY_MS`, with the safe triangle covering
        // the trip across the card -- see `hoverRowAttrs`.
        //
        // What used to make that too expensive was the fetch behind the Model row: pi shells
        // out to `pi --list-models` (up to 15s) and codex connects to its daemon, and on hover
        // that fired on every pointer sweep. It no longer can -- `warmOfferedModels` runs at
        // most once per card-open, and the card's own open already warms it.
        ...(opts.openable
          ? hoverRowAttrs({ which: opts.which, onOpen: opts.onOpen })
          : hoverRowAttrs({ which: null })),
        // Click still toggles, and is the only way in on a touch screen, where there is no
        // hover to intend anything with.
        onclick: (event: MouseEvent) => {
          if (!opts.openable) return;
          cancelHoverIntent();
          const opening = flyout !== opts.which;
          openFlyoutFromRow(opening ? opts.which : null, event.currentTarget as HTMLElement, opts.onOpen);
        },
      },
      [
        m("span", { class: css.ROW_LABEL }, opts.label),
        m("span", { class: css.ROW_VALUE }, [
          m("span", { class: css.ROW_TEXT }, opts.value),
          opts.sub !== undefined ? m("span", { class: css.ROW_SUBTEXT }, `(${opts.sub})`) : null,
          // No chevron when there is nothing to drill into. A disclosure arrow on a row that
          // opens nothing is a promise the card cannot keep.
          opts.openable ? m("span", { class: css.ROW_CHEVRON }, m.trust(icon("chevron-right", { size: 13 }))) : null,
        ]),
      ],
    );
    return row;
  }

  /** The effort slider, or null when there is nothing to slide.
   *
   * Two deliberate choices, both decided rather than discovered:
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
  function effortRow(opts: {
    efforts: readonly { level: string; in_picker: boolean }[];
    current: string | null;
    interactive: boolean;
    tooltip: string | null;
    onPick: (level: string) => void;
  }): m.Vnode | null {
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
    return m(
      "div",
      {
        class: css.ROW_STATIC,
        "data-card-row": "effort",
        ...tooltipAttrs(opts.tooltip),
        // Reaching for the slider is leaving the flyout behind, so it goes away -- the same
        // rule every row in the card follows.
        ...hoverRowAttrs({ which: null }),
      },
      [
        m("span", { class: css.ROW_LABEL }, "Effort"),
        m("span", { class: css.ROW_VALUE_STATIC }, [
          m("span", { class: css.EFFORT_VALUE }, capitalizeEffort(level)),
          m("span", { class: css.SLIDER_WRAP }, [
            // A dot at each level: without them the slider is a bare line and the levels it can
            // land on are guesswork.
            m(
              "span",
              { class: css.SLIDER_TICKS },
              shown.map((effort) => m("span", { key: effort.level, class: css.SLIDER_TICK })),
            ),
            m("input", {
              type: "range",
              "aria-label": "Reasoning effort",
              class: css.SLIDER,
              min: 0,
              max: shown.length - 1,
              step: 1,
              disabled: !opts.interactive,
              // Mithril re-asserts `value` on every redraw, which would snap the thumb back
              // under the pointer mid-drag on any harness that does not move the chip
              // optimistically -- codex is exactly that. Holding the dragged index locally and
              // clearing it on release keeps the thumb where the finger is.
              value: position,
              style:
                `background: linear-gradient(to right, ${effortFillColor(pct / 100)} ${pct}%, ` +
                `var(--color-fill-active) ${pct}%)`,
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
      ],
    );
  }

  /** Fast mode: a switch.
   *
   * A switch rather than a toggling icon, because a switch says on or off by its shape instead
   * of by its fill.
   */
  function fastRow(opts: {
    on: boolean;
    interactive: boolean;
    tooltip: string | null;
    onToggle: () => void;
  }): m.Vnode {
    return m(
      "div",
      {
        class: css.ROW_STATIC,
        "data-card-row": "fast",
        ...tooltipAttrs(opts.tooltip),
        ...hoverRowAttrs({ which: null }),
      },
      [
        m("span", { class: css.ROW_LABEL }, "Fast Mode"),
        m(
          "span",
          { class: css.ROW_VALUE_STATIC },
          m(
            "button",
            {
              type: "button",
              role: "switch",
              class: `${css.SWITCH} ${opts.on ? css.SWITCH_ON : css.SWITCH_OFF}`,
              "aria-label": "Fast Mode",
              "aria-checked": opts.on ? "true" : "false",
              disabled: !opts.interactive,
              onclick: () => {
                if (opts.interactive) opts.onToggle();
              },
            },
            m(
              "span",
              { class: `${css.SWITCH_KNOB} ${opts.on ? css.SWITCH_KNOB_ON : css.SWITCH_KNOB_OFF}` },
              opts.on
                ? m("span", { class: css.SWITCH_CHECK }, m.trust(icon("check", { size: 12, strokeWidth: 3.5 })))
                : null,
            ),
          ),
        ),
      ],
    );
  }

  /** The card's viewport left, clamped so it cannot hang off either edge. */
  function cardLeft(anchor: DOMRect): number {
    return Math.min(
      Math.max(anchor.left, CARD_MARGIN),
      Math.max(CARD_MARGIN, window.innerWidth - CARD_MARGIN - css.CARD_WIDTH),
    );
  }

  /** The card hangs off the trigger's top-left corner, because the composer sits at the
   *  bottom of the panel. The width is set here rather than as a class
   *  because the rows are `w-full` -- a card left to size itself to its content gives them a
   *  click target only as wide as their own text. */
  function cardPlacement(anchor: DOMRect): string {
    return (
      `left: ${cardLeft(anchor)}px; bottom: ${window.innerHeight - anchor.top + 8}px; ` + `width: ${css.CARD_WIDTH}px;`
    );
  }

  /** Where a flyout sits: beside the card, standing on the row that opened it. */
  function flyoutPlacement(): string {
    const anchor = cardAnchor;
    if (anchor === null) return "";
    const placed = placeFlyout({
      cardLeft: cardLeft(anchor),
      cardWidth: css.CARD_WIDTH,
      rowBottom: flyoutRowBottom,
      flyoutWidth: css.FLYOUT_WIDTH,
      maxFlyoutHeight: css.FLYOUT_MAX_HEIGHT,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      margin: CARD_MARGIN,
      overlap: css.FLYOUT_OVERLAP,
    });
    return (
      `left: ${placed.left}px; bottom: ${placed.bottom}px; ` +
      `width: ${css.FLYOUT_WIDTH}px; max-height: ${placed.maxHeight}px;`
    );
  }

  /** The shell every flyout renders into, so both register the same outside-click element. */
  function flyoutShell(children: m.Children): m.Vnode {
    // The safe triangle's base is this box, and its height follows its contents -- so measure
    // on arrival AND on every redraw that changes them (a filtered list is shorter, and a
    // triangle pointing at the box's old bottom would guard rows nobody is heading through).
    const measure = (flyoutVnode: m.VnodeDOM): void => {
      flyoutRect = (flyoutVnode.dom as HTMLElement).getBoundingClientRect();
    };
    return m(
      "div",
      {
        class: css.FLYOUT,
        [POPOVER_ATTR]: "flyout",
        style: flyoutPlacement(),
        oncreate: measure,
        onupdate: measure,
        // Arrived. The trip is over, so the wedge that protected it closes and the card's rows
        // answer the pointer normally again the moment it goes back.
        onmouseenter: () => {
          cancelHoverIntent();
          clearSafeApex();
        },
      },
      children,
    );
  }

  /** The chat's reversible process verb: ``mngr stop`` on the agent, which a later message or
   *  start brings back. No confirmation -- it is one message away from undone. The agent list
   *  catches up through the observe stream. */
  function stopAgentRow(targetAgentId: string): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        class: css.ROW,
        "data-card-row": "stop-agent",
        ...hoverRowAttrs({ which: null }),
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          closeCard();
          void fetch(apiUrl(`/api/agents/${encodeURIComponent(targetAgentId)}/stop`), { method: "POST" })
            .then(async (response) => {
              if (response.ok) return;
              const data = (await response.json().catch(() => ({}))) as { detail?: string };
              alert(`Failed to stop the agent: ${data.detail ?? `HTTP ${response.status}`}`);
            })
            .catch((e: Error) => {
              alert(`Failed to stop the agent: ${e.message}`);
            });
        },
      },
      [m("span", { class: css.ROW_LABEL }, "Stop agent"), m("span", { class: css.ROW_VALUE }, "")],
    );
  }

  /** A row of the flyout that is still fetching its contents. */
  function loadingRow(): m.Vnode {
    return m("div", { class: `${css.FLYOUT_EMPTY} flex items-center gap-2` }, [
      m("span", { class: "pv-spinner" }),
      "Loading models...",
    ]);
  }

  /** The confirmation a pressed account row opens: launch a new chat on it, or not. */
  function launchPrompt(target: ProviderAccount, current: ProviderAccount | null): m.Children {
    return m(launchDialog, {
      title: "Launch a new chat?",
      body: [
        `A chat's provider is fixed when it starts, so this one stays on ${current?.label ?? "its provider"}. ` +
          `Start a new chat on ${target.label}?`,
      ],
      dismissLabel: "Cancel",
      actions: [
        {
          label: "Launch",
          run: () => {
            closeCard();
            void startChatOnAccount(target.id);
          },
        },
      ],
      onDismiss: () => {
        launchPromptAccountId = null;
      },
    });
  }

  /** The Provider row's menu: every signed-in account, plus a way to add one.
   *
   * Our chats bind to an account when they are created and nothing rebinds them, so pressing
   * an account that is not this chat's asks to open a new chat on it. Each row also carries
   * the default toggle: the starred account is the one a new chat opens on when nothing
   * names one (the New Tab tile, the rail shortcut, an agent's `layout.py open chat`).
   */
  function providerFlyout(current: ProviderAccount | null): m.Vnode {
    const rows = getAccounts();
    const defaultId = getDefaultAccountId();
    const prompted = rows.find((row) => row.id === launchPromptAccountId) ?? null;
    return flyoutShell([
      // Built as one list rather than with a conditional hole beside it: mithril refuses a
      // fragment that mixes keyed vnodes with a null, and every row here is keyed.
      m(
        "div",
        { class: css.FLYOUT_SCROLL },
        rows.length === 0
          ? [m("div", { class: css.FLYOUT_EMPTY }, "No providers yet.")]
          : rows.map((row) => {
              const isCurrent = current !== null && row.id === current.id;
              return accountRow({
                row,
                isCurrent,
                isDefault: row.id === defaultId,
                rowClass: isCurrent ? css.ACCOUNT_ROW_SELECTED : css.ACCOUNT_ROW,
                onSelect: () => {
                  if (isCurrent) {
                    setFlyout(null);
                    return;
                  }
                  launchPromptAccountId = row.id;
                },
                state: rowState,
              });
            }),
      ),
      prompted !== null ? launchPrompt(prompted, current) : null,
      m(
        "button",
        {
          type: "button",
          class: css.FLYOUT_ADD,
          onclick: () => {
            closeCard();
            openProviderChooser({ onSignedIn: (accountId) => startChatOnAccount(accountId) });
          },
        },
        "+ Add a provider",
      ),
    ]);
  }

  /** The Model row's menu: the models this account can actually use. */
  function modelFlyout(
    agentId: string,
    sourceOptions: readonly CatalogModelOption[],
    matched: CatalogModelOption | null,
    currentIdentity: ModelIdentity,
    optimistic: boolean,
    searchable: boolean,
    dynamic: boolean,
  ): m.Vnode {
    const offeredIds = searchable && offeredLoaded ? offeredModels : null;
    const all = sourceOptions
      .filter((option) => option.in_picker)
      .filter((option) => offeredIds === null || offeredIds.has(option.id));
    const query = modelQuery.trim().toLowerCase();
    const filtered = query === "" ? all : all.filter((option) => option.label.toLowerCase().includes(query));
    const visible = filtered.slice(0, MODEL_SEARCH_CAP);
    const loading = (searchable || dynamic) && (offeredLoading || !offeredLoaded);
    return flyoutShell([
      // One list or the other, never a hole beside keyed rows -- mithril refuses a fragment
      // that mixes the two, and it throws during the DOM diff rather than at build time.
      m(
        "div",
        { class: css.FLYOUT_SCROLL },
        loading
          ? [loadingRow()]
          : visible.length === 0
            ? [m("div", { class: css.FLYOUT_EMPTY }, "No models available.")]
            : visible.map((option) => {
                const isCurrent = matched !== null && option.id === matched.id;
                return m(
                  "button",
                  {
                    type: "button",
                    key: option.id,
                    class: isCurrent ? css.FLYOUT_ROW_SELECTED : css.FLYOUT_ROW,
                    onclick: () => {
                      const next: ModelIdentity = {
                        model_id: option.id,
                        effort: clampEffort(option, currentIdentity.effort),
                        fast: option.supports_fast ? currentIdentity.fast : false,
                      };
                      setModelChoice(agentId, next, option, changedAxes(currentIdentity, next), optimistic);
                      setFlyout(null);
                    },
                  },
                  [
                    m("span", { class: css.FLYOUT_ROW_NAME }, option.label),
                    isCurrent
                      ? m("span", { class: css.FLYOUT_CHECK }, m.trust(icon("check", { size: 13, strokeWidth: 2.5 })))
                      : null,
                  ],
                );
              }),
      ),
      // BELOW the list, not above it: the flyout is anchored at its base and grows upward, so
      // the bottom is the edge that stays put next to the row you came from.
      //
      // The shared input recipe, with the magnifier laid over its left padding: the field owns
      // its own frame and focus ring, so nothing here re-styles either.
      searchable || all.length > 8
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
    ]);
  }

  return {
    oninit() {
      // The catalogs are static and shared; load them once.
      void ensureHarnessCatalogs();
      document.addEventListener("mousedown", handleOutsideMousedown);
    },

    onremove() {
      document.removeEventListener("mousedown", handleOutsideMousedown);
      // A pending hover would otherwise fire into a torn-down component and redraw it.
      cancelHoverIntent();
      clearSafeApex();
    },

    view(vnode) {
      const agentId = vnode.attrs.agentId;
      const agent = getAgentById(agentId);
      const account = accountForAgent(agent?.labels?.account);
      const catalog: HarnessCatalog | null = getHarnessCatalog(agent?.harness);
      const choice = catalog === null ? null : effectiveChoice(agentId, agent?.model_choice);
      const matched = choice?.matched ?? null;

      // THREE states have no model, not one, and the Provider row must render in all of them:
      // the provider is a property of the ACCOUNT, not of the model. The catalog may not have
      // loaded; the live choice may not have resolved (every harness passes through this
      // before its first model read, and opencode never leaves it); or the live model may
      // match no catalog option. Only the Model/Effort/Fast rows are suppressed.
      if (agent === undefined) return null;
      // Nothing at all to say: no account to name and no model to show.
      if (account === null && matched === null) return null;

      // opencode ships an empty catalog and a resolver that fails, so its every pick would
      // 500. Read-only is the honest render -- a picker there offers a switch that cannot work.
      const readOnly = catalog === null || catalog.switch_mode === "read_only";
      const interactive = !readOnly && matched !== null;
      const optimistic = catalog?.switch_mode === "eager_then_reconcile";
      const currentEffort = choice?.identity.effort ?? null;
      const currentFast = choice?.identity.fast ?? false;
      const shownEfforts = (matched?.efforts ?? []).filter((effort) => effort.in_picker);
      const readOnlyTooltip = interactive ? null : READ_ONLY_TOOLTIP;

      // The chip states the WHOLE choice, from the same three values the card's rows read --
      // one source, so the summary and the detail cannot disagree. Effort appears only when
      // the model has one to state, and the bolt only when fast is actually on.
      const trigger = m(
        "button",
        {
          type: "button",
          // A stable hook for the composer's own styles and for tests.
          class: `model-selector-trigger ${css.TRIGGER}`,
          [POPOVER_ATTR]: "trigger",
          title: "Model, effort and speed",
          "aria-expanded": cardAnchor !== null ? "true" : "false",
          onclick: (event: MouseEvent) => {
            event.stopPropagation();
            if (cardAnchor !== null) {
              closeCard();
              return;
            }
            openCard(event.currentTarget as HTMLElement);
            // Warm the model list the moment the CARD opens, not when the flyout does: the
            // fetch is the slow part (pi shells out to `pi --list-models`), and by the time a
            // pointer has crossed the card it is usually already back. With hover-opened
            // flyouts this is what makes the Model row cheap to pass over -- by the time the
            // hover lands, the list is warm and its own request is a no-op.
            if (catalog?.picker_mode === "search" || catalog?.picker_mode === "dynamic") {
              warmOfferedModels(agentId);
            }
          },
        },
        [
          // Joined by dots between EVERY part, including before the bolt: the three axes are
          // one reading, and a bolt tacked on without a separator read as a button.
          m("span", matched?.label ?? account?.provider ?? "Model"),
          shownEfforts.length > 1 && currentEffort !== null
            ? [m("span", { class: css.TRIGGER_DOT }, "·"), m("span", capitalizeEffort(currentEffort))]
            : null,
          currentFast
            ? [
                m("span", { class: css.TRIGGER_DOT }, "·"),
                m("span", { class: "flex items-center" }, m.trust(icon("zap", { size: 12, filled: true }))),
              ]
            : null,
        ],
      );

      if (cardAnchor === null) return m("div", { class: "model-bar" }, trigger);

      const currentIdentity: ModelIdentity =
        matched === null
          ? { model_id: "", effort: currentEffort, fast: currentFast }
          : { model_id: matched.id, effort: currentEffort, fast: currentFast };

      const searchable = catalog?.picker_mode === "search";
      const dynamic = catalog?.picker_mode === "dynamic";
      const sourceOptions: CatalogModelOption[] = dynamic ? (dynamicOptions ?? []) : (catalog?.options ?? []);

      const card = m(
        "div",
        {
          class: css.CARD,
          [POPOVER_ATTR]: "card",
          style: cardPlacement(cardAnchor),
        },
        m("div", { class: css.CARD_INNER }, [
          menuRow({
            label: "Provider",
            value: account?.provider ?? "Not signed in",
            sub: account?.harness_label,
            which: "providers",
            openable: true,
            tooltip: null,
          }),
          m("div", { class: css.DIVIDER }),
          matched !== null
            ? menuRow({
                label: "Model",
                value: matched.label,
                which: "model",
                // A read-only harness gets a row that states the model and nothing more: no
                // chevron, no list. Its models are switched from its own terminal.
                openable: interactive,
                tooltip: readOnlyTooltip,
                onOpen: () => {
                  if (searchable || dynamic) warmOfferedModels(agentId);
                },
              })
            : null,
          matched !== null
            ? effortRow({
                efforts: matched.efforts,
                current: currentEffort,
                interactive,
                tooltip: readOnlyTooltip,
                onPick: (level) => {
                  const next: ModelIdentity = { model_id: matched.id, effort: level, fast: currentFast };
                  setModelChoice(agentId, next, matched, changedAxes(currentIdentity, next), optimistic);
                },
              })
            : null,
          matched !== null && matched.supports_fast
            ? fastRow({
                on: currentFast,
                interactive,
                tooltip: readOnlyTooltip,
                onToggle: () => {
                  const next: ModelIdentity = { model_id: matched.id, effort: currentEffort, fast: !currentFast };
                  setModelChoice(agentId, next, matched, changedAxes(currentIdentity, next), optimistic);
                },
              })
            : null,
          m("div", { class: css.DIVIDER }),
          stopAgentRow(agentId),
        ]),
      );

      const openFlyout =
        flyout === "providers"
          ? providerFlyout(account)
          : flyout === "model"
            ? modelFlyout(agentId, sourceOptions, matched, currentIdentity, optimistic, searchable, dynamic)
            : null;

      // The card and its flyout PORTAL to <body>. The chat panel lives inside dockview's
      // clipping overlay, so a card that extends past the panel would be cut off at its edge.
      return [m("div", { class: "model-bar" }, trigger), m(Portal, { children: [card, openFlyout] })];
    },
  };
}
