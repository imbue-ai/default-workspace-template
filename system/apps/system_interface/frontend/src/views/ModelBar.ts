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
import { accountForAgent, getAccounts, openProviderChooser } from "../models/Providers";
import type { ProviderAccount } from "../models/Providers";
import { startChatOnAccount } from "./DockviewWorkspace";
import { placeFlyout } from "./flyout-position";
import { icon } from "./icons";
import * as css from "./modelCardStyles";

/** Shown on hover for any read-only model/effort/fast slot: a read-only harness's model is
 *  switched from the agent's own terminal (its native picker), not from this bar. */
const READ_ONLY_TOOLTIP = "Use agent terminal to switch models";

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
  let flyoutRowTop = 0;
  let triggerElement: HTMLElement | null = null;
  let cardElement: HTMLElement | null = null;
  let flyoutElement: HTMLElement | null = null;
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

  /** Open the card, anchored to the trigger that was clicked. */
  function openCard(trigger: HTMLElement): void {
    cardAnchor = trigger.getBoundingClientRect();
    flyout = null;
    modelQuery = "";
  }

  function closeCard(): void {
    cardAnchor = null;
    flyout = null;
  }

  function handleOutsideMousedown(event: MouseEvent): void {
    const target = event.target as Node;
    const insideCard = cardElement !== null && cardElement.contains(target);
    const insideFlyout = flyoutElement !== null && flyoutElement.contains(target);
    const insideTrigger = triggerElement !== null && triggerElement.contains(target);
    if (!insideCard && !insideFlyout && !insideTrigger) {
      closeCard();
      m.redraw();
    }
  }

  /** One card row that opens a flyout. */
  function menuRow(opts: {
    label: string;
    value: string;
    sub?: string;
    which: "model" | "providers";
    onOpen?: () => void;
  }): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        class: css.ROW,
        // CLICK, not hover. The mockup opens these on `onMouseEnter`, which is free in a
        // prototype and expensive here: opening the model flyout fetches this agent's
        // offerable models, which for pi shells out to `pi --list-models` (up to 15s) and
        // for codex connects to its daemon. On hover that fires on every pointer sweep
        // across the card. Clicking also makes the mockup's safe-triangle hover-aim
        // machinery unnecessary, which is ~60 lines of slope math not ported.
        onclick: (event: MouseEvent) => {
          flyoutRowTop = (event.currentTarget as HTMLElement).getBoundingClientRect().top;
          const opening = flyout !== opts.which;
          flyout = opening ? opts.which : null;
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
          m("span", { class: css.ROW_CHEVRON }, m.trust(icon("chevron-right", { size: 13 }))),
        ]),
      ],
    );
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
    return m("div", { class: css.ROW_STATIC }, [
      m("span", { class: css.ROW_LABEL }, "Effort"),
      m("span", { class: css.ROW_VALUE_STATIC }, [
        m("span", { class: css.EFFORT_VALUE }, capitalizeEffort(opts.current ?? shown[index].level)),
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
          style: `background: linear-gradient(to right, ${effortFillColor(pct / 100)} ${pct}%, var(--color-fill-active) ${pct}%)`,
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
    ]);
  }

  /** Where the card sits: above its trigger, left-aligned, clamped to the viewport. */
  function cardPlacement(anchor: DOMRect): string {
    const width = 300;
    const margin = 8;
    const left = Math.min(Math.max(anchor.left, margin), Math.max(margin, window.innerWidth - margin - width));
    // Above the trigger, because the composer sits at the bottom of the panel.
    return `left: ${left}px; bottom: ${window.innerHeight - anchor.top + 6}px;`;
  }

  /** Where a flyout sits: beside the card, top-aligned with the row that opened it. */
  function flyoutPlacement(): string {
    const anchor = cardAnchor;
    if (anchor === null) return "";
    const margin = 8;
    const width = 280;
    const left = Math.min(Math.max(anchor.left, margin), Math.max(margin, window.innerWidth - margin - width));
    const placed = placeFlyout({
      parent: { left, right: left + 300 },
      rowTop: flyoutRowTop,
      flyoutWidth: width,
      maxFlyoutHeight: 420,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      margin,
      overlap: 4,
    });
    return `left: ${placed.left}px; top: ${placed.top}px; max-height: ${placed.maxHeight}px;`;
  }

  /** The Provider row's menu: every signed-in account, plus a way to add one.
   *
   * Every account that is not this chat's is LOCKED. Our chats bind to an account when they
   * are created and nothing rebinds them, so there is no state in which switching would work
   * -- clicking one opens a new chat on it instead, which is what the user meant.
   */
  function providerFlyout(agentId: string, current: ProviderAccount | null): m.Vnode {
    const rows = getAccounts();
    return m(
      "div",
      {
        class: css.FLYOUT,
        style: flyoutPlacement(),
        oncreate: (flyoutVnode: m.VnodeDOM) => {
          flyoutElement = flyoutVnode.dom as HTMLElement;
        },
        onremove: () => {
          flyoutElement = null;
        },
      },
      [
        // Built as one list rather than with a conditional hole beside it: mithril refuses a
        // fragment that mixes keyed vnodes with a null, and every row here is keyed.
        m(
          "div",
          { class: css.FLYOUT_SCROLL },
          rows.length === 0
            ? [m("div", { class: css.FLYOUT_EMPTY }, "No providers yet.")]
            : rows.map((row) => {
                const isCurrent = current !== null && row.id === current.id;
                return m(
                  "button",
                  {
                    type: "button",
                    key: row.id,
                    class: isCurrent ? css.FLYOUT_ROW : css.FLYOUT_ROW_LOCKED,
                    "data-tooltip": isCurrent
                      ? undefined
                      : "Opens a new chat -- this one is already running on its provider",
                    onclick: () => {
                      if (isCurrent) {
                        flyout = null;
                        return;
                      }
                      closeCard();
                      startChatOnAccount(row.id);
                    },
                  },
                  [
                    m("span", { class: css.FLYOUT_ROW_NAME }, row.provider),
                    m("span", { class: css.FLYOUT_ROW_SUB }, `(${row.harness_label})`),
                    isCurrent ? m("span", { class: css.FLYOUT_CHECK }, m.trust(icon("check", { size: 14 }))) : null,
                  ],
                );
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
      ],
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
    const placeholder = loading ? "Loading models..." : visible.length === 0 ? "No models available." : null;
    return m(
      "div",
      {
        class: css.FLYOUT,
        style: flyoutPlacement(),
        oncreate: (flyoutVnode: m.VnodeDOM) => {
          flyoutElement = flyoutVnode.dom as HTMLElement;
        },
        onremove: () => {
          flyoutElement = null;
        },
      },
      [
        // Searchable only when there are enough rows to need it -- pi's catalog runs to
        // thousands, claude's to four.
        searchable || all.length > 8
          ? m("input", {
              class: "model-selector-search",
              type: "text",
              placeholder: "Search models",
              value: modelQuery,
              oncreate: (inputVnode: m.VnodeDOM) => (inputVnode.dom as HTMLInputElement).focus(),
              oninput: (event: Event) => {
                modelQuery = (event.target as HTMLInputElement).value;
              },
            })
          : null,
        // One list or the other, never a hole beside keyed rows -- mithril refuses a fragment
        // that mixes the two, and it throws during the DOM diff rather than at build time.
        m(
          "div",
          { class: css.FLYOUT_SCROLL },
          placeholder !== null
            ? [m("div", { class: css.FLYOUT_EMPTY }, placeholder)]
            : visible.map((option) =>
                m(
                  "button",
                  {
                    type: "button",
                    key: option.id,
                    class: css.FLYOUT_ROW,
                    onclick: () => {
                      const next: ModelIdentity = {
                        model_id: option.id,
                        effort: clampEffort(option, currentIdentity.effort),
                        fast: option.supports_fast ? currentIdentity.fast : false,
                      };
                      setModelChoice(agentId, next, option, changedAxes(currentIdentity, next), optimistic);
                      flyout = null;
                    },
                  },
                  [
                    m("span", { class: css.FLYOUT_ROW_NAME }, option.label),
                    matched !== null && option.id === matched.id
                      ? m("span", { class: css.FLYOUT_CHECK }, m.trust(icon("check", { size: 14 })))
                      : null,
                  ],
                ),
              ),
        ),
      ],
    );
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
      const interactive = catalog !== null && catalog.switch_mode !== "read_only" && matched !== null;
      const optimistic = catalog?.switch_mode === "eager_then_reconcile";
      const currentEffort = choice?.identity.effort ?? null;
      const currentFast = choice?.identity.fast ?? false;

      const trigger = m(
        "button",
        {
          type: "button",
          class: css.TRIGGER,
          "data-tooltip": interactive ? "Provider and model" : READ_ONLY_TOOLTIP,
          "aria-expanded": cardAnchor !== null ? "true" : "false",
          oncreate: (triggerVnode: m.VnodeDOM) => {
            triggerElement = triggerVnode.dom as HTMLElement;
          },
          onremove: () => {
            triggerElement = null;
          },
          onclick: (event: MouseEvent) => {
            event.stopPropagation();
            if (cardAnchor !== null) {
              closeCard();
              return;
            }
            openCard(event.currentTarget as HTMLElement);
          },
        },
        [
          m("span", { class: css.TRIGGER_LABEL }, matched?.label ?? account?.provider ?? "Model"),
          m("span", { class: css.TRIGGER_SUB }, m.trust(icon("chevron-down", { size: 12 }))),
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
          style: cardPlacement(cardAnchor),
          oncreate: (cardVnode: m.VnodeDOM) => {
            cardElement = cardVnode.dom as HTMLElement;
          },
          onremove: () => {
            cardElement = null;
          },
        },
        [
          menuRow({
            label: "Provider",
            value: account?.provider ?? "Not signed in",
            sub: account?.harness_label,
            which: "providers",
          }),
          m("div", { class: css.DIVIDER }),
          matched !== null
            ? menuRow({
                label: "Model",
                value: matched.label,
                which: "model",
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
                onPick: (level) => {
                  const next: ModelIdentity = { model_id: matched.id, effort: level, fast: currentFast };
                  setModelChoice(agentId, next, matched, changedAxes(currentIdentity, next), optimistic);
                },
              })
            : null,
          matched !== null && matched.supports_fast
            ? m("div", { class: css.ROW_STATIC }, [
                m(
                  "span",
                  { class: `${css.ROW_LABEL} ${css.FAST_LABEL}${interactive ? "" : ` ${css.FAST_LABEL_OFF}`}` },
                  [
                    m("span", { class: currentFast ? "text-accent" : "" }, m.trust(icon("zap", { size: 12 }))),
                    "Fast mode",
                  ],
                ),
                m(
                  "span",
                  { class: css.ROW_VALUE_STATIC },
                  m(
                    "button",
                    {
                      type: "button",
                      class: `fast-toggle${currentFast ? " fast-toggle--on" : ""}${interactive ? "" : " fast-toggle--readonly"}`,
                      "aria-label": currentFast ? "Disable fast mode" : "Enable fast mode",
                      "aria-pressed": currentFast ? "true" : "false",
                      onclick: () => {
                        if (!interactive) return;
                        const next: ModelIdentity = {
                          model_id: matched.id,
                          effort: currentEffort,
                          fast: !currentFast,
                        };
                        setModelChoice(agentId, next, matched, changedAxes(currentIdentity, next), optimistic);
                      },
                    },
                    m.trust(icon("zap", { size: 14 })),
                  ),
                ),
              ])
            : null,
        ],
      );

      const openFlyout =
        flyout === "providers"
          ? providerFlyout(agentId, account)
          : flyout === "model"
            ? modelFlyout(agentId, sourceOptions, matched, currentIdentity, optimistic, searchable, dynamic)
            : null;

      // The card and its flyout PORTAL to <body>. The chat panel lives inside dockview's
      // clipping overlay, so a card that extends past the panel would be cut off at its edge.
      return [m("div", { class: "model-bar" }, trigger), m(Portal, { children: [card, openFlyout] })];
    },
  };
}

/** Renders its children into <body>.
 *
 * The chat panel sits inside dockview's `overflow: hidden` overlay, so a card or flyout that
 * extends past the panel is clipped at its edge. Mithril has no portal, so this mounts a
 * detached root and renders into it -- the same shape `lightbox.ts` and `hoverTooltip.ts` use.
 */
function Portal(): m.Component<{ children: m.Children }> {
  let host: HTMLElement | null = null;
  return {
    onremove() {
      if (host !== null) {
        m.render(host, null);
        host.remove();
        host = null;
      }
    },
    view(vnode) {
      // Created here rather than in `oncreate`: the view runs first, so a host made there
      // would be empty on the pass that mattered and nothing would schedule another.
      if (host === null) {
        host = document.createElement("div");
        document.body.appendChild(host);
      }
      m.render(host, vnode.attrs.children);
      return null;
    },
  };
}
