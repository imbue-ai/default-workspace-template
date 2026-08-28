/**
 * The composer model bar: [Logo][Model][Effort][Fast].
 *
 * A self-contained component so it can sit wherever the layout wants it (its own
 * row below the chat input, next to the terminal/auth actions). Everything it
 * shows is data -- the static per-harness catalog (logo, options, efforts, switch
 * mode) from HarnessCatalog.ts, plus the agent's live `model_choice` pushed onto
 * the agents store. Which slots show is decided purely by the matched catalog
 * option (effort iff the model declares efforts; fast iff it supports fast); the
 * switch mode only decides whether the shown slots are interactive. A pick is
 * applied optimistically and reconciled from the pushed live choice.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { getAgentById } from "../models/AgentManager";
import type { CatalogModelOption, HarnessCatalog } from "../models/HarnessCatalog";
import { ensureHarnessCatalogs, getHarnessCatalog } from "../models/HarnessCatalog";
import { changedAxes, effectiveChoice, setModelChoice } from "../models/ModelSettings";
import type { ModelIdentity } from "../models/ModelSettings";
import { Button } from "./Button";
import { clampDropdownLeft } from "./dropdown-position";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon } from "./icons";

/** Shown on hover for any read-only model/effort/fast slot: a read-only harness's model is
 *  switched from the agent's own terminal (its native picker), not from this bar. */
const READ_ONLY_TOOLTIP = "Use agent terminal to switch models";

/* ── Styling ──────────────────────────────────────────────────────────────────
 * Utilities in the markup; the model-* class names stay as bare markers
 * (positionDropdown queries .model-selector-label/-dropdown-header, and the
 * e2e tests find the bar by its markers). States are resolved in code, one
 * utility per property. */

/** The slot trigger. h-[30px] is the composer under-bar's control height. A
 *  read-only slot is NOT `disabled` (that would suppress its hover tooltip):
 *  it renders at normal weight/color with no hover brighten and a default
 *  cursor, and no-ops on click. */
const TRIGGER_BASE =
  "model-selector-trigger inline-flex h-[30px] items-center gap-1 rounded-lg border-none bg-transparent px-2 " +
  "font-sans text-(length:--font-size-body) whitespace-nowrap text-secondary select-none " +
  "transition-colors duration-(--dur-base)";
const TRIGGER_INTERACTIVE = "cursor-pointer hover:bg-fill-hover hover:text-primary";
const TRIGGER_READONLY = "model-selector-trigger--readonly cursor-default";

/** The popup card, anchored above the trigger; positionDropdown() then nudges
 *  it with a translateX. min/max width bound it so a long provider/model tag
 *  can't grow the menu off the screen edge; options past the max ellipsize. */
const DROPDOWN_CLASS =
  "model-selector-dropdown absolute bottom-[calc(100%+8px)] left-0 z-(--z-sticky) min-w-[200px] " +
  "max-w-[min(92vw,460px)] overflow-hidden rounded-lg border bg-surface p-1.5 shadow-overlay";

/** One option row: 29px tall, 8 of them visible before the list scrolls
 *  (max-h below = 8 rows). */
const OPTION_BASE =
  "model-selector-option block h-[29px] cursor-pointer truncate rounded-md px-2.5 font-sans " +
  "text-(length:--font-size-body) leading-[29px] transition-colors duration-(--dur-fast)";
const LIST_CLASS =
  "model-selector-dropdown-list max-h-[232px] overflow-y-auto overscroll-contain pr-1 " +
  "supports-[-moz-appearance:none]:pr-3 [scrollbar-color:var(--c-border)_transparent]";

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

export function ModelBar(): m.Component<{ agentId: string }> {
  // Which dropdown is open (model or effort, or none) and the bar element used to
  // detect an outside click closing it.
  let openDropdown: "model" | "effort" | null = null;
  let barElement: HTMLElement | null = null;
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

  function handleOutsideMousedown(event: MouseEvent): void {
    if (barElement !== null && !barElement.contains(event.target as Node)) {
      openDropdown = null;
      m.redraw();
    }
  }

  // The margin every popup keeps between itself and each screen edge.
  const DROPDOWN_MARGIN = 8;

  // The live viewport listeners/observer that keep the OPEN dropdown positioned. A
  // single dropdown is open at a time (see `openDropdown`), so one set suffices; they
  // are wired in the dropdown's `oncreate` and torn down in its `onremove`.
  let dropdownResizeObserver: ResizeObserver | null = null;
  let dropdownViewportListener: (() => void) | null = null;

  // Position an open popup horizontally: align its inner text under the trigger's label
  // text when there is room, and clamp so the whole box stays on-screen with a margin.
  // The dropdown is left-anchored to its wrapper (`left: 0`); we translateX from there.
  //
  // Robust to size/layout changes AFTER open -- the searchable picker grows once its async
  // model list resolves, and a dockview split/resize can move the trigger with no redraw --
  // because it re-measures the live rects every time it runs, and it runs on every redraw
  // (`onupdate`), on any dropdown resize (ResizeObserver), and on window resize. Clearing
  // the transform before measuring keeps it idempotent across those repeated calls.
  function positionDropdown(dom: HTMLElement): void {
    const wrapper = dom.parentElement;
    if (wrapper === null) {
      return;
    }
    dom.style.transform = "";
    const dropdownRect = dom.getBoundingClientRect();
    const label = wrapper.querySelector(".model-selector-label");
    const header = dom.querySelector(".model-selector-dropdown-header");
    // Fall back to the dropdown's own left when either reference is somehow absent, so a
    // missing node yields no shift rather than throwing (both always render in practice).
    const labelLeft = label === null ? dropdownRect.left : label.getBoundingClientRect().left;
    // Measure the text inset from the DOM (header text left minus the box's left) rather
    // than hard-coding padding, so it stays correct if the styling changes. The header's own
    // left padding is added because its border-box left only reaches the dropdown's padding,
    // not the text; without it the dropdown would sit ~10px right of the trigger label.
    const textInset =
      header === null
        ? 0
        : header.getBoundingClientRect().left - dropdownRect.left + parseFloat(getComputedStyle(header).paddingLeft);
    const targetLeft = clampDropdownLeft({
      labelLeft,
      textInset,
      dropdownWidth: dropdownRect.width,
      viewportWidth: window.innerWidth,
      margin: DROPDOWN_MARGIN,
    });
    const shift = targetLeft - dropdownRect.left;
    if (shift !== 0) {
      dom.style.transform = `translateX(${shift}px)`;
    }
  }

  // Attach the live re-position listeners for a freshly opened dropdown.
  function attachDropdownPositioning(dom: HTMLElement): void {
    dropdownViewportListener = () => positionDropdown(dom);
    window.addEventListener("resize", dropdownViewportListener);
    // The ResizeObserver's initial notification (delivered before the next paint) is
    // redundant with the direct positionDropdown() call in oncreate; its real job is to
    // re-fire when the async model list later changes the dropdown's size.
    dropdownResizeObserver = new ResizeObserver(() => positionDropdown(dom));
    dropdownResizeObserver.observe(dom);
  }

  // Tear the listeners down when the dropdown closes.
  function detachDropdownPositioning(): void {
    if (dropdownViewportListener !== null) {
      window.removeEventListener("resize", dropdownViewportListener);
      dropdownViewportListener = null;
    }
    if (dropdownResizeObserver !== null) {
      dropdownResizeObserver.disconnect();
      dropdownResizeObserver = null;
    }
  }

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

  function renderDropdown(opts: {
    kind: "model" | "effort";
    triggerLabel: string;
    header: string;
    items: { id: string; label: string }[];
    selectedId: string | null;
    interactive: boolean;
    tooltip: string;
    searchable?: boolean;
    // Re-fetch the offer set every time this picker opens (search + dynamic pickers). A static
    // list picker leaves this false and renders the catalog directly.
    refetchOnOpen?: boolean;
    loading?: boolean;
    onOpen?: () => void;
    onPick: (id: string) => void;
  }): m.Vnode {
    const isOpen = openDropdown === opts.kind;
    // For a search picker, filter by the query and cap the rendered rows; otherwise
    // show every option. Filtering on `label` (== the provider/model tag).
    const query = modelQuery.trim().toLowerCase();
    const filtered = opts.searchable
      ? opts.items.filter((item) => item.label.toLowerCase().includes(query))
      : opts.items;
    const visible = opts.searchable ? filtered.slice(0, MODEL_SEARCH_CAP) : filtered;
    const hiddenCount = filtered.length - visible.length;
    return m("div", { class: "model-selector-wrapper relative inline-block" }, [
      m(
        "button",
        {
          type: "button",
          class: `${TRIGGER_BASE} ${opts.interactive ? TRIGGER_INTERACTIVE : TRIGGER_READONLY}`,
          // A read-only slot is NOT `disabled`: a disabled button suppresses hover events, which
          // would kill the tooltip. It renders normally, shows the switch-in-terminal tooltip,
          // and no-ops on click instead.
          ...hoverTooltipAttrs(opts.interactive ? opts.tooltip : READ_ONLY_TOOLTIP),
          onclick: (event: MouseEvent) => {
            event.stopPropagation();
            if (!opts.interactive) return;
            const opening = !isOpen;
            openDropdown = isOpen ? null : opts.kind;
            // Reset the search and re-fetch the offer set each time a search/dynamic picker opens.
            if (opening && opts.refetchOnOpen) {
              modelQuery = "";
              opts.onOpen?.();
            }
          },
        },
        [
          m("span", { class: "model-selector-label" }, opts.triggerLabel),
          // No chevron on a read-only slot -- it isn't a dropdown.
          opts.interactive
            ? m(
                "span",
                { class: "model-selector-chevron inline-flex items-center text-faint" },
                m.trust(icon("chevron-down", { size: 12 })),
              )
            : null,
        ],
      ),
      isOpen && opts.interactive
        ? m(
            "div",
            {
              class: DROPDOWN_CLASS,
              oncreate: (v: m.VnodeDOM) => {
                document.addEventListener("mousedown", handleOutsideMousedown);
                positionDropdown(v.dom as HTMLElement);
                attachDropdownPositioning(v.dom as HTMLElement);
              },
              onupdate: (v: m.VnodeDOM) => positionDropdown(v.dom as HTMLElement),
              onremove: () => {
                document.removeEventListener("mousedown", handleOutsideMousedown);
                detachDropdownPositioning();
              },
            },
            [
              m(
                "div",
                {
                  class:
                    "model-selector-dropdown-header px-2.5 pt-1 pb-1.5 font-sans text-(length:--font-size-helper) font-medium text-faint",
                },
                opts.header,
              ),
              opts.searchable
                ? m(
                    "div",
                    {
                      class:
                        "model-selector-search mx-0.5 mb-1.5 flex h-[30px] items-center gap-1.5 rounded-lg border " +
                        "bg-fill-hover px-2 transition-colors duration-(--dur-base) focus-within:border-accent focus-within:bg-surface",
                    },
                    [
                      m(
                        "span",
                        { class: "model-selector-search-icon inline-flex flex-none items-center text-faint" },
                        m.trust(icon("search", { size: 14 })),
                      ),
                      m("input", {
                        class:
                          "model-selector-search-input min-w-0 flex-auto border-none bg-transparent font-sans " +
                          "text-(length:--font-size-body) text-primary outline-none placeholder:text-faint",
                        type: "text",
                        placeholder: "Search models…",
                        value: modelQuery,
                        oncreate: (v: m.VnodeDOM) => (v.dom as HTMLInputElement).focus(),
                        oninput: (e: InputEvent) => {
                          modelQuery = (e.target as HTMLInputElement).value;
                        },
                      }),
                    ],
                  )
                : null,
              opts.loading
                ? m("div", { class: "model-selector-more" }, "Loading models…")
                : m(
                    "ul",
                    { class: LIST_CLASS },
                    visible.map((item) =>
                      m(
                        "li",
                        {
                          key: item.id,
                          title: item.label,
                          // The selected row's accent tint persists under hover
                          // (no hover restyle); unselected rows brighten.
                          class: `${OPTION_BASE} ${
                            opts.selectedId === item.id
                              ? "model-selector-option--selected bg-accent-light font-medium text-accent"
                              : "text-primary hover:bg-fill-hover"
                          }`,
                          onclick: () => {
                            openDropdown = null;
                            if (opts.selectedId !== item.id) {
                              opts.onPick(item.id);
                            }
                          },
                        },
                        item.label,
                      ),
                    ),
                  ),
              !opts.loading && hiddenCount > 0
                ? m("div", { class: "model-selector-more" }, `+${hiddenCount} more — keep typing to narrow`)
                : null,
              !opts.loading && opts.refetchOnOpen && visible.length === 0
                ? m("div", { class: "model-selector-more" }, "No matching models")
                : null,
            ],
          )
        : null,
    ]);
  }

  return {
    oninit() {
      // The catalogs are static and shared; load them once.
      void ensureHarnessCatalogs();
    },
    view(vnode) {
      const agentId = vnode.attrs.agentId;
      const agent = getAgentById(agentId);
      const catalog: HarnessCatalog | null = getHarnessCatalog(agent?.harness);
      if (catalog === null) {
        // No catalog (feature-flagged off, or catalogs not loaded yet): no slots.
        return null;
      }

      // This component renders ONLY the model/effort/fast slots. The harness "Powered by"
      // credit lives on its own per-agent path (PoweredByCredit, in the composer actions row),
      // so this bar is free to return null before a choice resolves without taking it down.
      const choice = effectiveChoice(agentId, agent?.model_choice);
      if (choice === null) {
        // The live selection has not resolved yet; render no slots.
        return null;
      }
      const matched = choice.matched;
      if (matched === null) {
        // The current combo matches no catalog option: a shrug, no model/effort/fast.
        return m("div", { class: "model-bar inline-flex items-center gap-0.5" }, [
          m(
            "span",
            {
              class:
                "model-bar-shrug inline-flex h-[30px] items-center px-1.5 text-(length:--font-size-body) text-secondary select-none",
              ...hoverTooltipAttrs("Unrecognized model"),
            },
            "\u{1F937}",
          ),
        ]);
      }

      // Interactive for any switchable harness -- a pending pick never disables the
      // bar. `optimistic` (an EAGER_THEN_RECONCILE harness moves the chip on click)
      // governs whether a pick shows immediately or waits for the pushed live choice;
      // all three harnesses (claude, codex, pi) are EAGER_THEN_RECONCILE.
      const interactive = catalog.switch_mode !== "read_only";
      // Only EAGER_THEN_RECONCILE moves the chip optimistically on click. codex is ON_CHANGE:
      // interactive, but the chip waits for the pushed (confirmed) live choice -- no overlay.
      const optimistic = catalog.switch_mode === "eager_then_reconcile";
      const currentEffort = choice.identity.effort;
      const currentFast = choice.identity.fast;
      // The value a pick is diffed against: the matched option's catalog id (NOT
      // choice.identity.model_id, which is the raw reported id), so changedAxes counts a
      // model change iff the picked catalog id differs -- an effort/fast click keeps this id.
      const currentIdentity: ModelIdentity = { model_id: matched.id, effort: currentEffort, fast: currentFast };

      // Where the picker's model rows come from, by picker mode:
      //  - "dynamic" (codex): the FULL per-agent options fetched on open -- there is NO static
      //    catalog, so `dynamicOptions` IS the source (empty until the first fetch resolves).
      //  - "search" (pi): the static catalog, narrowed to the account-gated ids fetched on open.
      //  - "list" (claude): the static catalog verbatim.
      // A search/dynamic picker re-fetches on every open and shows a loading row while in flight.
      const searchable = catalog.picker_mode === "search";
      const dynamic = catalog.picker_mode === "dynamic";
      const refetchOnOpen = searchable || dynamic;
      const sourceOptions: CatalogModelOption[] = dynamic ? (dynamicOptions ?? []) : catalog.options;
      const offeredIds = searchable && offeredLoaded ? offeredModels : null;
      const modelItems = sourceOptions
        .filter((option) => option.in_picker)
        .filter((option) => offeredIds === null || offeredIds.has(option.id))
        .map((option) => ({ id: option.id, label: option.label }));

      const modelSlot = renderDropdown({
        kind: "model",
        triggerLabel: matched.label,
        header: "Model",
        items: modelItems,
        selectedId: matched.id,
        interactive,
        tooltip: "Select model",
        searchable,
        refetchOnOpen,
        loading: refetchOnOpen && (offeredLoading || !offeredLoaded),
        onOpen: () => void fetchOfferedModels(agentId),
        onPick: (modelId) => {
          const option = sourceOptions.find((candidate) => candidate.id === modelId);
          if (option === undefined) {
            return;
          }
          // Clamp effort into the new model's declared set, and drop fast if the new
          // model does not support it (the backend validates the same).
          const nextIdentity: ModelIdentity = {
            model_id: option.id,
            effort: clampEffort(option, currentEffort),
            fast: option.supports_fast ? currentFast : false,
          };
          setModelChoice(agentId, nextIdentity, option, changedAxes(currentIdentity, nextIdentity), optimistic);
        },
      });

      const shownEfforts = matched.efforts.filter((effort) => effort.in_picker);
      const effortSlot =
        matched.efforts.length > 0
          ? renderDropdown({
              kind: "effort",
              triggerLabel: currentEffort === null ? "Effort" : capitalizeEffort(currentEffort),
              header: "Effort",
              items: shownEfforts.map((effort) => ({ id: effort.level, label: capitalizeEffort(effort.level) })),
              selectedId: currentEffort,
              interactive,
              tooltip: "Select reasoning effort",
              onPick: (level) => {
                const nextIdentity: ModelIdentity = { model_id: matched.id, effort: level, fast: currentFast };
                setModelChoice(agentId, nextIdentity, matched, changedAxes(currentIdentity, nextIdentity), optimistic);
              },
            })
          : null;

      // The on/off control is the shared Button as a selectable ghost;
      // `readonly` (not `disabled`, which would kill the tooltip) is the
      // read-only treatment, same as the triggers above.
      const fastSlot = matched.supports_fast
        ? m(
            Button,
            {
              variant: "ghost",
              icon: true,
              sm: true,
              selected: currentFast,
              readonly: !interactive,
              extra: "model-fast-toggle shrink-0",
              ...hoverTooltipAttrs(
                !interactive ? READ_ONLY_TOOLTIP : currentFast ? "Disable fast mode" : "Enable fast mode",
              ),
              "aria-label": currentFast ? "Disable fast mode" : "Enable fast mode",
              "aria-pressed": currentFast ? "true" : "false",
              onclick: () => {
                if (!interactive) return;
                const nextIdentity: ModelIdentity = {
                  model_id: matched.id,
                  effort: currentEffort,
                  fast: !currentFast,
                };
                setModelChoice(agentId, nextIdentity, matched, changedAxes(currentIdentity, nextIdentity), optimistic);
              },
            },
            m.trust(icon("zap", { size: 16 })),
          )
        : null;

      return m(
        "div",
        {
          class: "model-bar inline-flex items-center gap-0.5",
          oncreate: (barVnode: m.VnodeDOM) => {
            barElement = barVnode.dom as HTMLElement;
          },
          onremove: () => {
            barElement = null;
          },
        },
        [modelSlot, effortSlot, fastSlot],
      );
    },
  };
}
