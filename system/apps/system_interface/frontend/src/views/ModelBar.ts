/**
 * The composer's combo card: which PROVIDER this chat runs on, and which model on it.
 *
 * Ported from the mockup (`imbue-ai/mind-sketches`, `prototypes/minds-harness`). Replaces the
 * old three-slot bar, whose slots said nothing about the provider -- which used to be
 * invisible because there was only ever one.
 *
 * Everything it shows is data: the static per-harness catalog from HarnessCatalog.ts, the
 * agent's live `model_choice` pushed onto the agents store, and its `account` label resolved
 * against the account list. Which rows show is decided by the matched catalog option (effort
 * iff the model declares more than one; fast iff it supports it); the switch mode decides
 * whether they are interactive.
 *
 * The provider row is the one that always renders. A provider is a property of the ACCOUNT,
 * not of the model, so it survives all three of the states in which there is no model to show.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { getAgentById } from "../models/AgentManager";
import type { CatalogModelOption, HarnessCatalog } from "../models/HarnessCatalog";
import { ensureHarnessCatalogs, getHarnessCatalog } from "../models/HarnessCatalog";
import { changedAxes, effectiveChoice, setModelChoice } from "../models/ModelSettings";
import type { ModelIdentity } from "../models/ModelSettings";
import { accountForAgent, deleteAccount, getAccounts, openProviderChooser } from "../models/Providers";
import type { ProviderAccount } from "../models/Providers";
import { startChatOnAccount } from "./DockviewWorkspace";
import { placeFlyout } from "./flyout-position";
import { Portal } from "./portal";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon } from "./icons";
import * as css from "./modelCardStyles";

/** Shown on a read-only harness's rows. agy's `/model` is an interactive TUI with no
 *  scriptable form, so the card cannot drive it -- and says where the user can. */
const READ_ONLY_TOOLTIP = "To change the model or effort, run /model or /effort in the agent terminal.";

/** Shown on a provider row this chat cannot switch to. */
const LOCKED_PROVIDER_TOOLTIP =
  "Start a new chat to use this provider. In-chat provider switching is coming soon!";

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

/** The slider's filled portion, deepening with effort. From the mockup verbatim. */
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
  // Which account's trash has been armed into "Remove?". Cleared whenever the flyout or the
  // card closes, so someone who clicked the bin to see what it did does not come back later
  // to a primed one.
  let confirmingRemoval: string | null = null;
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


  function openCard(trigger: HTMLElement): void {
    cardAnchor = trigger.getBoundingClientRect();
    setFlyout(null);
    modelQuery = "";
  }

  function closeCard(): void {
    cardAnchor = null;
    setFlyout(null);
  }

  /** The ONLY way the open flyout changes.
   *
   *  An armed "Remove?" belongs to the submenu it was armed in and must outlive everything
   *  short of that submenu going away -- another row being pressed, the pointer wandering off,
   *  a redraw. Routing every change through here is what makes "until the submenu closes"
   *  true by construction rather than by remembering to clear it at four call sites. */
  function setFlyout(next: "model" | "providers" | null): void {
    if (next !== flyout) confirmingRemoval = null;
    flyout = next;
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
        // The mockup's own row hook, kept so the two can be diffed and so a test can address
        // a row by what it is rather than by its classes.
        "data-card-row": opts.which,
        ...tooltipAttrs(opts.tooltip),
        // CLICK, not hover. The mockup opens these on `onMouseEnter`, which is free in a
        // prototype and expensive here: opening the model flyout fetches this agent's
        // offerable models, which for pi shells out to `pi --list-models` (up to 15s) and
        // for codex connects to its daemon. On hover that fires on every pointer sweep
        // across the card. Clicking also makes the mockup's safe-triangle hover-aim
        // machinery unnecessary, which is ~60 lines of slope math not ported.
        onclick: (event: MouseEvent) => {
          if (!opts.openable) return;
          flyoutRowBottom = (event.currentTarget as HTMLElement).getBoundingClientRect().bottom;
          const opening = flyout !== opts.which;
          setFlyout(opening ? opts.which : null);
          if (opening) {
            modelQuery = "";
            opts.onOpen?.();
          }
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
   * Two deliberate divergences from the mockup, both decided rather than discovered:
   *
   * 1. `onchange`, not `oninput`. The mockup's React `onChange` maps to the DOM `input`
   *    event, which fires once per notch passed during a drag -- and every notch here is a
   *    live switch typed into the agent's pane (claude), a socket call (codex) or a parked
   *    intent (pi). `setModelChoice` chains rather than debounces, so a low-to-max drag
   *    would queue four sequential switches. This commits once, on release.
   * 2. Indexed over the SHOWN list, accepting that an agent on a hidden level (claude's
   *    `ultra`) pins its thumb at the far left. The LABEL still reads correctly, because it
   *    comes from the value rather than the position.
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
    const index = Math.max(
      0,
      shown.findIndex((effort) => effort.level === opts.current),
    );
    const pct = (index / (shown.length - 1)) * 100;
    return m("div", { class: css.ROW_STATIC, ...tooltipAttrs(opts.tooltip) }, [
      m("span", { class: css.ROW_LABEL }, "Effort"),
      m("span", { class: css.ROW_VALUE_STATIC }, [
        m("span", { class: css.EFFORT_VALUE }, capitalizeEffort(opts.current ?? shown[index].level)),
        m("span", { class: css.SLIDER_WRAP }, [
          // Tick marks behind the track: without them the slider is a bare line and the levels
          // it can land on are guesswork. Not in the mockup; asked for after using it.
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
            value: draggingEffortIndex ?? index,
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
    ]);
  }

  /** Fast mode: a switch, ported from the mockup's `Switch.tsx`.
   *
   * The bolt that used to sit here doubled as the label AND the control, so its state had to be
   * read off its fill -- a switch says on or off by its shape.
   */
  function fastRow(opts: { on: boolean; interactive: boolean; tooltip: string | null; onToggle: () => void }): m.Vnode {
    return m("div", { class: css.ROW_STATIC, ...tooltipAttrs(opts.tooltip) }, [
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
            opts.on ? m("span", { class: css.SWITCH_CHECK }, m.trust(icon("check", { size: 12, strokeWidth: 3.5 }))) : null,
          ),
        ),
      ),
    ]);
  }

  /** The card's viewport left, clamped so it cannot hang off either edge. */
  function cardLeft(anchor: DOMRect): number {
    return Math.min(
      Math.max(anchor.left, CARD_MARGIN),
      Math.max(CARD_MARGIN, window.innerWidth - CARD_MARGIN - css.CARD_WIDTH),
    );
  }

  /** The mockup's `above`: the card hangs off the trigger's top-left corner, because the
   *  composer sits at the bottom of the panel. The width is set here rather than as a class
   *  because the rows are `w-full` -- a card left to size itself to its content gives them a
   *  click target only as wide as their own text. */
  function cardPlacement(anchor: DOMRect): string {
    return (
      `left: ${cardLeft(anchor)}px; bottom: ${window.innerHeight - anchor.top + 8}px; ` +
      `width: ${css.CARD_WIDTH}px;`
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
    return m(
      "div",
      {
        class: css.FLYOUT,
        [POPOVER_ATTR]: "flyout",
        style: flyoutPlacement(),
      },
      children,
    );
  }

  /** A row of the flyout that is still fetching its contents. */
  function loadingRow(): m.Vnode {
    return m("div", { class: `${css.FLYOUT_EMPTY} flex items-center gap-2` }, [
      m("span", { class: "pv-spinner" }),
      "Loading models...",
    ]);
  }

  /** The Provider row's menu: every signed-in account, plus a way to add one.
   *
   * Every account that is not this chat's is LOCKED. Our chats bind to an account when they
   * are created and nothing rebinds them, so there is no state in which switching would work
   * -- clicking one opens a new chat on it instead, which is what the user meant.
   */
  function providerFlyout(current: ProviderAccount | null): m.Vnode {
    const rows = getAccounts();
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
              const arming = confirmingRemoval === row.id;
              return m("div", { key: row.id, class: css.ROW_WRAP }, [
                m(
                  "button",
                  {
                    type: "button",
                    class: isCurrent ? css.FLYOUT_ROW_SELECTED : css.FLYOUT_ROW_LOCKED,
                    "aria-disabled": isCurrent ? undefined : "true",
                    ...tooltipAttrs(isCurrent ? null : LOCKED_PROVIDER_TOOLTIP),
                    onclick: () => {
                      // Locked, and locked means locked: a chat's account is fixed at create
                      // time. Opening a new chat on it is the reachable version of the wish,
                      // but it is a different act, so it is the tooltip's offer -- not
                      // something a click on a disabled-looking row does by surprise.
                      if (isCurrent) setFlyout(null);
                    },
                  },
                  [
                    m("span", { class: css.FLYOUT_ROW_NAME }, row.provider),
                    m("span", { class: css.FLYOUT_ROW_SUB }, `(${row.harness_label})`),
                    isCurrent ? m("span", { class: css.FLYOUT_CHECK }, m.trust(icon("check", { size: 13, strokeWidth: 2.5 }))) : null,
                  ],
                ),
                removalControl(row, arming),
              ]);
            }),
      ),
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

  /** The per-row sign-out control: a trash can that arms into "Remove?" rather than firing on
   *  the first press. Signing out is not undoable, and the bin only appears on hover, so a
   *  single click would too often be someone finding out what it was. */
  function removalControl(row: ProviderAccount, arming: boolean): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        class: arming ? css.ROW_TRASH_ARMED : css.ROW_TRASH,
        "aria-label": arming ? `Confirm removing ${row.provider}` : `Sign out of ${row.provider}`,
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          if (!arming) {
            confirmingRemoval = row.id;
            return;
          }
          confirmingRemoval = null;
          void deleteAccount(row.id);
        },
      },
      arming ? "Remove?" : m.trust(icon("trash", { size: 13 })),
    );
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
                    isCurrent ? m("span", { class: css.FLYOUT_CHECK }, m.trust(icon("check", { size: 13, strokeWidth: 2.5 }))) : null,
                  ],
                );
              }),
      ),
      // BELOW the list, not above it: the flyout is anchored at its base and grows upward, so
      // the bottom is the edge that stays put next to the row you came from.
      //
      // The wrapper is the styled control; the bare input inside carries `outline: none`.
      // Putting the wrapper's class straight on an <input> is what left the browser's own
      // focus ring showing through, which is the orange halo in the report.
      searchable || all.length > 8
        ? m("div", { class: "model-selector-search" }, [
            m("span", { class: "model-selector-search-icon" }, m.trust(icon("search", { size: 13 }))),
            m("input", {
              class: "model-selector-search-input",
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
          // A stable hook for the composer's own styles and for tests; the tailwind classes
          // beside it are the mockup's and may be re-ported at any time.
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
            // pointer has crossed the card it is usually already back.
            if (catalog?.picker_mode === "search" || catalog?.picker_mode === "dynamic") {
              void fetchOfferedModels(agentId);
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
                  if (searchable || dynamic) void fetchOfferedModels(agentId);
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
