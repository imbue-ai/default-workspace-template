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
 * While a switch is armed the menu describes the TARGET instead, since that is what the next
 * message runs on.
 *
 * How the menu opens, closes and grows its submenus is the workspace `Menu`'s
 * (`components/menu`), not this file's: it opens on a click of the chip, its submenus open on
 * hover, and it is dismissed the way it was summoned. What this file owns is the rows -- the
 * effort slider, the fast-mode row, the account list with its controls, the model list with its
 * search -- and the data behind them.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { getChatById } from "../models/Chats";
import type { CatalogModelOption, HarnessCatalog } from "../models/HarnessCatalog";
import { ensureHarnessCatalogs, getHarnessCatalog } from "../models/HarnessCatalog";
import { ensureFastModeState, fastModeLabel, getFastModeState } from "../models/FastMode";
import { changedAxes, effectiveChoice, setModelChoice } from "../models/ModelSettings";
import type { ModelIdentity } from "../models/ModelSettings";
import {
  getPendingAccountId,
  getPendingPick,
  isSwitchTarget,
  pendingSwitchTarget,
  setPendingAccount,
  switchKind,
} from "../models/PendingLane";
import { accountForAgent, getAccounts, getDefaultAccountId, openProviderChooser } from "../models/Providers";
import { beginSwitchTo, openSwitchDialog } from "./SwitchDialog";
import type { ProviderAccount } from "../models/Providers";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { createMenu, menuRowClass, startTruncated, type MenuRow } from "@imbue/workspace-ui/src/components/menu";
import { Portal } from "@imbue/workspace-ui/src/portal";
import { accountRow, emptyAccountRowState } from "./accountRow";
import { FastModeModal } from "./FastModeModal";
import * as css from "./modelProviderMenuStyles";

/** Shown on a read-only harness's rows. agy's `/model` is an interactive TUI with no
 *  scriptable form, so the menu cannot drive it -- and says where the user can. */
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
  return level.charAt(0).toUpperCase() + level.slice(1);
}

/** How many rows a searched model list shows at most. Past this a query, not a scroll, is the
 *  way to the model. */
const MODEL_SEARCH_CAP = 100;

/** The slider's filled portion, deepening with effort: 70% lightness at the bottom of the
 *  scale, 40% at the top. Darker than 40% reads as near-black rather than as a deep green. */
function effortFillColor(fraction: number): string {
  return `hsl(152 39% ${Math.round(70 - 30 * fraction)}%)`;
}

export function ModelProviderMenu(): m.Component<{ chatId: string }> {
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
  // Whether this open of the menu has already fetched its offerable models. The menu's own
  // open warms them, and with hover-opened submenus a pointer crossing the Model row would
  // otherwise re-run a `pi --list-models` that takes up to 15s. Fresh per open is what
  // matters -- a /login between two opens still shows up -- so this resets with the menu.
  let offeredFetchedForOpen = false;
  // The account rows' own transient state -- an armed "Remove?", an open rename field.
  // Cleared whenever the submenu or the menu closes, so someone who clicked the bin to see
  // what it did does not come back later to a primed one.
  const rowState = emptyAccountRowState();
  // The index the pointer is currently dragging the effort slider to. Held locally because
  // mithril re-asserts `value` on every redraw, which would snap the thumb back under the
  // finger on a harness that does not move the chip optimistically.
  let draggingEffortIndex: number | null = null;
  // The fast-mode chooser is up (models/FastMode.ts); opened from the fast row, which closes
  // the menu, so it outlives the menu it was opened from.
  let isFastModeModalOpen = false;
  // What the last view saw, for the menu's own open hook to read: which chat is showing, and
  // whether its picker is the kind whose model list is worth warming.
  let viewedChatId = "";
  let viewedPickerIsFetched = false;

  /** What is scoped to a submenu: reset whenever the open submenu changes. */
  function resetSubmenuState(): void {
    rowState.confirmingRemoval = null;
    rowState.renamingId = null;
    rowState.renameDraft = "";
  }

  const menu = createMenu({
    // The menu hangs off the chip's top edge, because the composer sits at the bottom of the
    // panel and there is nothing under it to grow into.
    placement: "above",
    width: css.MENU_WIDTH,
    // A stable hook for tests, and for the composer's own styles.
    extraClass: "model-provider-menu",
    onOpen: () => {
      modelQuery = "";
      offeredFetchedForOpen = false;
      // Warm the model list the moment the MENU opens, not when the submenu does: the fetch is
      // the slow part (pi shells out to `pi --list-models`), and by the time a pointer has
      // crossed the menu it is usually already back. With hover-opened submenus this is what
      // makes the Model row cheap to pass over -- by the time the hover lands, the list is warm
      // and its own request is a no-op.
      if (viewedPickerIsFetched) warmOfferedModels(viewedChatId);
    },
    onClose: () => {
      // A drag that never released (the menu can be torn down mid-gesture) would otherwise
      // still be driving the label and the thumb the next time the menu opens.
      draggingEffortIndex = null;
      resetSubmenuState();
    },
    onSubmenuChange: () => {
      resetSubmenuState();
      modelQuery = "";
    },
    // A rename mid-type or an armed "Remove?" (providers), and a typed search (model), are all
    // work a drifting pointer must not throw away.
    holdsSubmenuOpen: (key) => {
      if (key === "providers") {
        return rowState.renamingId !== null || rowState.confirmingRemoval !== null;
      }
      return modelQuery !== "";
    },
  });

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
    } catch {
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
  }): m.Children {
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
          // A dot at each level: without them the slider is a bare line and the levels it can
          // land on are guesswork. Every level EXCEPT the one the thumb is on -- there the ball
          // is the mark, and it is dropped from the list rather than hidden in place, because a
          // keyed list may not carry holes.
          //
          // A dot takes the colour of what it is drawn ON, which is the fill below the thumb
          // and the bare track above it: one mark in two colours reads as a scale the fill is
          // swallowing, where one colour throughout reads as dots disappearing under it.
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
            // Mithril re-asserts `value` on every redraw, which would snap the thumb back
            // under the pointer mid-drag on any harness that does not move the chip
            // optimistically -- codex is exactly that. Holding the dragged index locally and
            // clearing it on release keeps the thumb where the finger is.
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

  /** A row that states a value and opens a chooser of its OWN -- a dialog, not a submenu, which
   *  is the one shape the shared menu has no kind for. Dressed as the submenu rows beside it:
   *  the shared row, its label a step back from its value, and a chevron saying there is
   *  somewhere to go. The menu closes on the way, because what opens is a modal over it. */
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

  /** Fast mode, as a row that opens the chooser. The chooser is a modal of its own so the menu
   *  stays a list of one-line facts. */
  function fastRow(opts: { chatId: string; tooltip: string | null }): m.Vnode {
    return pickerRow({
      label: "Fast Mode",
      value: fastModeValue(opts.chatId),
      tooltip: opts.tooltip,
      onOpen: () => {
        isFastModeModalOpen = true;
      },
    });
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
   * a rebind (an account on the chat's own harness and lane); run at once for a chat with no
   * user turn yet. Pressing the armed account again, or the account the chat runs on, takes the
   * choice back. Each row also carries the default toggle: the starred
   * account is the one a new chat opens on when nothing names one (the New Tab tile, the rail
   * shortcut, an agent's `layout.py open chat`).
   */
  function providerSubmenu(chatId: string, current: ProviderAccount | null): m.Children {
    const rows = getAccounts();
    const defaultId = getDefaultAccountId();
    const pendingId = getPendingAccountId(chatId);
    const chat = getChatById(chatId);
    // The account rows (or the one line standing in for them when there are none), plus the
    // "+ Add a provider" row under them.
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
              const isPending = row.id === pendingId;
              return accountRow({
                row,
                isCurrent,
                isDefault: row.id === defaultId,
                rowClass: isCurrent ? css.ACCOUNT_ROW_SELECTED : css.ACCOUNT_ROW,
                ...(isPending ? { badge: "next" } : {}),
                onSelect: () => {
                  if (isCurrent || isPending || chat === undefined || !isSwitchTarget(chat, row)) {
                    menu.closeSubmenu();
                    setPendingAccount(chatId, null);
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
              onSignedIn: (accountId) => {
                // Signed in from inside a chat: the new account is what the user switches this
                // chat to next, so the switch begins on it.
                const account = accountForAgent(accountId);
                if (account !== null) beginSwitchTo(chatId, account);
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
      // ABOVE the list: the field is where the pointer arrives and where the typing starts, so
      // it sits at the head of the submenu rather than under a list it filters. It stays put
      // while the list scrolls beneath it.
      //
      // The shared input recipe, with the magnifier laid over its left padding: the field owns
      // its own frame and focus ring, so nothing here re-styles either.
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
                        effort: clampEffort(option, currentIdentity.effort),
                        fast: option.supports_fast ? currentIdentity.fast : false,
                      };
                      setModelChoice(chatId, next, option, changedAxes(currentIdentity, next), optimistic);
                      // The pick is done; the menu stays, with the new model's effort and fast
                      // rows there to adjust.
                      menu.closeSubmenu();
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
      if (chat === undefined) return null;
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
      const searchable = catalog?.picker_mode === "search";
      const dynamic = catalog?.picker_mode === "dynamic";
      viewedChatId = chatId;
      viewedPickerIsFetched = searchable || dynamic;

      // The account the next send switches the chat to, and the model picked for it: while a
      // switch is armed the menu reads as the target, since that is what the next message runs on.
      const pending = pendingSwitchTarget(chatId);
      const pendingPick = getPendingPick(chatId);

      // The chip states the WHOLE choice, from the same three values the menu's rows read --
      // one source, so the summary and the detail cannot disagree. Effort appears only when
      // the model has one to state, and the bolt only when fast is actually on. An armed switch
      // replaces all of it with the target's pick and a "next" mark.
      const trigger = m(
        "button",
        {
          type: "button",
          // A stable hook for the composer's own styles and for tests.
          class: `model-selector-trigger ${css.TRIGGER}`,
          ...menu.triggerAttrs(),
          // The workspace's own bubble, not a native `title`: one tooltip mechanism everywhere
          // (and this one can say what the button DOES, where a native title is stuck reading
          // as a label for what is already written on the chip).
          ...hoverTooltipAttrs("Change model or provider", "above"),
        },
        pending !== null
          ? [
              m("span", pendingPick?.label ?? pending.harness_label),
              m(
                "span",
                {
                  class:
                    "model-provider-menu-next-badge ml-1.5 rounded-full bg-accent-light px-1.5 " +
                    "text-(length:--font-size-helper) text-accent",
                },
                "next",
              ),
            ]
          : [
              // A dot joins the text parts; the bolt stands on the row's gap alone, since a glyph
              // is already read apart from the words and a dot beside it doubles the punctuation.
              m("span", matched?.label ?? account?.provider ?? "Model"),
              shownEfforts.length > 1 && currentEffort !== null
                ? [m("span", { class: css.TRIGGER_DOT }, "·"), m("span", capitalizeEffort(currentEffort))]
                : null,
              currentFast
                ? m("span", { class: "flex items-center" }, m.trust(icon("zap", { size: 12, filled: true })))
                : null,
            ],
      );

      // The fast-mode chooser outlives the menu it was opened from (the row closes the menu), so
      // it portals on its own while the menu is down.
      const fastModeModal = isFastModeModalOpen
        ? m(Portal, {
            children: m(FastModeModal, {
              chatId,
              onClose: () => {
                isFastModeModalOpen = false;
              },
            }),
          })
        : null;

      if (!menu.isOpen()) return [trigger, fastModeModal];

      const currentIdentity: ModelIdentity =
        matched === null
          ? { model_id: "", effort: currentEffort, fast: currentFast }
          : { model_id: matched.id, effort: currentEffort, fast: currentFast };
      const sourceOptions: CatalogModelOption[] = dynamic ? (dynamicOptions ?? []) : (catalog?.options ?? []);

      const rows: MenuRow[] = [];
      if (pending !== null) {
        // While a switch is armed the menu describes the TARGET: the account the next send moves
        // the chat to and, for a handoff, the model picked for it, which opens the dialog again
        // to change. A rebind keeps the agent's model, so it has no row to offer.
        rows.push({
          kind: "submenu",
          key: "providers",
          label: "Provider",
          value: pending.provider,
          sub: `${pending.harness_label}, next message`,
          content: () => providerSubmenu(chatId, account),
        });
        if (switchKind(chat, pending) === "handoff") {
          rows.push({ kind: "divider" });
          rows.push({
            kind: "custom",
            key: "model",
            render: () =>
              pickerRow({
                label: "Model",
                value: pendingPick?.label ?? "Default model",
                tooltip: "Change the model this chat switches to",
                onOpen: () => openSwitchDialog(chatId, pending),
              }),
          });
        }
      } else {
        rows.push({
          kind: "submenu",
          key: "providers",
          label: "Provider",
          value: account?.provider ?? "Not signed in",
          sub: account?.harness_label,
          content: () => providerSubmenu(chatId, account),
        });
        rows.push({ kind: "divider" });
        if (matched !== null) {
          // A read-only harness gets a row that states the model and nothing more: no chevron,
          // no list. Its models are switched from its own terminal.
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
              effortRow({
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
            // Read-only leaves the mode stated and nothing to press, the same way the Model row
            // stands down: the chooser would offer a switch the harness cannot take.
            rows.push(
              interactive
                ? { kind: "custom", key: "fast", render: () => fastRow({ chatId, tooltip: readOnlyTooltip }) }
                : {
                    kind: "value",
                    key: "fast",
                    label: "Fast Mode",
                    value: fastModeValue(chatId),
                    tooltip: readOnlyTooltip ?? undefined,
                  },
            );
          }
        }
      }
      rows.push({ kind: "divider" });
      rows.push({ kind: "action", key: "stop-agent", label: "Stop agent", onSelect: () => stopAgent(chatId) });

      return [trigger, menu.view(rows), fastModeModal];
    },
  };
}
