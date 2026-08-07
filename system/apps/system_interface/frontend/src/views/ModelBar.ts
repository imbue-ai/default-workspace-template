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
import { getAgentById } from "../models/AgentManager";
import type { CatalogModelOption, HarnessCatalog } from "../models/HarnessCatalog";
import { ensureHarnessCatalogs, getHarnessCatalog } from "../models/HarnessCatalog";
import { changedAxes, effectiveChoice, setModelChoice } from "../models/ModelSettings";
import type { ModelIdentity } from "../models/ModelSettings";
import { icon } from "./icons";

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

// A search picker (pi/opencode) can carry thousands of models; never lay out more than
// this many <li> at once. The user narrows with the search box; the cap bounds the DOM.
const MODEL_SEARCH_CAP = 100;

export function ModelBar(): m.Component<{ agentId: string }> {
  // Which dropdown is open (model or effort, or none) and the bar element used to
  // detect an outside click closing it.
  let openDropdown: "model" | "effort" | null = null;
  let barElement: HTMLElement | null = null;
  // The current model-search query (only used when the harness's picker_mode is "search").
  let modelQuery = "";

  function handleOutsideMousedown(event: MouseEvent): void {
    if (barElement !== null && !barElement.contains(event.target as Node)) {
      openDropdown = null;
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
    return m("div", { class: "model-selector-wrapper" }, [
      m(
        "button",
        {
          type: "button",
          class: "model-selector-trigger",
          disabled: !opts.interactive,
          "data-tooltip": opts.tooltip,
          onclick: (event: MouseEvent) => {
            event.stopPropagation();
            const opening = !isOpen;
            openDropdown = isOpen ? null : opts.kind;
            // Reset the search each time the picker opens.
            if (opening && opts.searchable) {
              modelQuery = "";
            }
          },
        },
        [
          m("span", { class: "model-selector-label" }, opts.triggerLabel),
          m("span", { class: "model-selector-chevron" }, m.trust(icon("chevron-down", { size: 12 }))),
        ],
      ),
      isOpen && opts.interactive
        ? m(
            "div",
            {
              class: "model-selector-dropdown",
              oncreate: () => document.addEventListener("mousedown", handleOutsideMousedown),
              onremove: () => document.removeEventListener("mousedown", handleOutsideMousedown),
            },
            [
              m("div", { class: "model-selector-dropdown-header" }, opts.header),
              opts.searchable
                ? m("input", {
                    class: "model-selector-search",
                    type: "text",
                    placeholder: "Search models…",
                    value: modelQuery,
                    oncreate: (v: m.VnodeDOM) => (v.dom as HTMLInputElement).focus(),
                    oninput: (e: InputEvent) => {
                      modelQuery = (e.target as HTMLInputElement).value;
                    },
                  })
                : null,
              m(
                "ul",
                { class: "model-selector-dropdown-list" },
                visible.map((item) =>
                  m(
                    "li",
                    {
                      key: item.id,
                      class:
                        "model-selector-option" +
                        (opts.selectedId === item.id ? " model-selector-option--selected" : ""),
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
              hiddenCount > 0
                ? m("div", { class: "model-selector-more" }, `+${hiddenCount} more — keep typing to narrow`)
                : null,
              opts.searchable && visible.length === 0
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
        // No catalog (feature-flagged off, or catalogs not loaded yet): no bar.
        return null;
      }
      const logo = m("span", { class: "model-bar-logo", "aria-hidden": "true" }, m.trust(catalog.icon_svg));

      const choice = effectiveChoice(agentId, agent?.model_choice);
      if (choice === null) {
        // The live selection has not resolved yet; show the logo alone.
        return m("div", { class: "model-bar" }, logo);
      }
      const matched = choice.matched;
      if (matched === null) {
        // The current combo matches no catalog option: a shrug, no model/effort/fast.
        return m("div", { class: "model-bar" }, [
          logo,
          m("span", { class: "model-bar-shrug", "data-tooltip": "Unrecognized model" }, "\u{1F937}"),
        ]);
      }

      // Interactive for any switchable harness -- a pending pick never disables the
      // bar. `optimistic` (only an EAGER harness moves the chip on click) governs
      // whether a pick shows immediately or waits for the pushed live choice: EAGER
      // (claude) is optimistic; ON_CHANGE (codex) reconciles from the rollout only.
      const interactive = catalog.switch_mode !== "read_only";
      const optimistic = catalog.switch_mode === "eager_then_reconcile";
      const currentEffort = choice.identity.effort;
      const currentFast = choice.identity.fast;

      const modelSlot = renderDropdown({
        kind: "model",
        triggerLabel: matched.label,
        header: "Model",
        items: catalog.options
          .filter((option) => option.in_picker)
          .map((option) => ({ id: option.id, label: option.label })),
        selectedId: matched.id,
        interactive,
        tooltip: "Select model",
        searchable: catalog.picker_mode === "search",
        onPick: (modelId) => {
          const option = catalog.options.find((candidate) => candidate.id === modelId);
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
          setModelChoice(agentId, nextIdentity, option, changedAxes(choice.identity, nextIdentity), optimistic);
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
                setModelChoice(agentId, nextIdentity, matched, changedAxes(choice.identity, nextIdentity), optimistic);
              },
            })
          : null;

      const fastSlot = matched.supports_fast
        ? m(
            "button",
            {
              type: "button",
              class: `fast-toggle${currentFast ? " fast-toggle--on" : ""}`,
              disabled: !interactive,
              "data-tooltip": currentFast ? "Disable fast mode" : "Enable fast mode",
              "aria-label": currentFast ? "Disable fast mode" : "Enable fast mode",
              "aria-pressed": currentFast ? "true" : "false",
              onclick: () => {
                const nextIdentity: ModelIdentity = {
                  model_id: matched.id,
                  effort: currentEffort,
                  fast: !currentFast,
                };
                setModelChoice(agentId, nextIdentity, matched, changedAxes(choice.identity, nextIdentity), optimistic);
              },
            },
            m.trust(icon("zap", { size: 16 })),
          )
        : null;

      return m(
        "div",
        {
          class: "model-bar",
          oncreate: (barVnode: m.VnodeDOM) => {
            barElement = barVnode.dom as HTMLElement;
          },
          onremove: () => {
            barElement = null;
          },
        },
        [logo, modelSlot, effortSlot, fastSlot],
      );
    },
  };
}
