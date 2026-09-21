/**
 * The Getting Started page (launcher-and-getting-started plan section 3.6), top to bottom: a
 * search field over its own content; "Start something", the eight intent tiles; "Start from a
 * template", the catalog's shelves. Picking a template shows its detail as a page inside this one,
 * with a way back. Typing swaps the sections for results: the matching intents and the matching
 * templates, laid out as a grid of cards. Every tile and both detail actions start a chat with a
 * seeded text through the one callback the page is given (``shell:start-with-text``); the page
 * names no app.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { CatalogTemplate, TemplateCatalogState } from "../models/TemplateCatalog";
import { resolveShelves, searchTemplates } from "../models/TemplateCatalog";
import { TemplateDetail } from "./TemplateDetail";
import { TemplateCard, TemplateShelves } from "./TemplateShelves";
import { HOVER_GLYPH_GROUP, HOVER_SHADOW_SELF } from "./hoverLift";
import {
  START_OPTIONS,
  START_PAGE_SIZE,
  hasMoreStartOptions,
  nextStartCount,
  searchStartOptions,
  startGlyph,
  visibleStartOptions,
} from "./startSomething";
import type { StartOption } from "./startSomething";

const SEARCH_PLACEHOLDER = "Search things to start and templates";
const START_SOMETHING_TITLE = "Start something";
const TEMPLATES_TITLE = "Start from a template";
const SEARCH_TEMPLATES_TITLE = "Templates";
const SEE_MORE_LABEL = "See more";
const TEMPLATES_LOADING_MESSAGE = "Loading templates…";
const TEMPLATES_FAILED_MESSAGE = "Failed to load templates.";

const SECTION_HEADING_CLASS = "type-section text-faint";
const START_GLYPH_SIZE = 24;
const FIELD_GLYPH_SIZE = 14;

export interface GettingStartedPageAttrs {
  readonly catalog: TemplateCatalogState;
  /** Start a chat whose first message is ``text``. */
  readonly onStartWithText: (text: string) => void;
}

export function GettingStartedPage(): m.Component<GettingStartedPageAttrs> {
  let query = "";
  let startShownCount = START_PAGE_SIZE;
  let detailTemplate: CatalogTemplate | null = null;
  let isScrollToTemplatesPending = false;

  function startTile(option: StartOption, attrs: GettingStartedPageAttrs): m.Vnode {
    const isCatalogOffered = attrs.catalog.kind !== "disabled";
    // The template tile has nowhere to scroll to without a catalog.
    const isDisabled = option.prompt === null && !isCatalogOffered;
    const pick = (): void => {
      if (option.prompt === null) {
        isScrollToTemplatesPending = true;
        return;
      }
      attrs.onStartWithText(option.prompt);
    };
    return m(
      "button",
      {
        key: option.key,
        type: "button",
        "data-start": option.key,
        "aria-disabled": isDisabled ? "true" : undefined,
        class:
          "getting-started-tile flex h-full flex-col rounded-xl border border-default bg-surface p-4 text-left " +
          (isDisabled ? "cursor-not-allowed text-faint" : `${HOVER_SHADOW_SELF} group cursor-pointer text-primary`),
        onclick: isDisabled ? undefined : pick,
      },
      [
        m(
          "span",
          { class: "flex shrink-0 items-center" + (isDisabled ? " text-faint" : ` ${HOVER_GLYPH_GROUP}`) },
          m.trust(startGlyph(option, START_GLYPH_SIZE, !isDisabled)),
        ),
        m("span", { class: "type-label mt-3 block" }, option.title),
        m(
          "span",
          {
            class:
              "type-helper mt-1 block " +
              (isDisabled
                ? "text-faint"
                : "text-secondary transition-colors duration-300 ease-out group-hover:text-primary"),
          },
          option.description,
        ),
      ],
    );
  }

  function startSomethingSection(
    options: readonly StartOption[],
    attrs: GettingStartedPageAttrs,
    footer: m.Vnode | null,
  ): m.Vnode {
    return m("section", { "data-section": "start-something", class: "getting-started-start mt-6 first:mt-0" }, [
      m("h2", { class: `${SECTION_HEADING_CLASS} mb-2` }, START_SOMETHING_TITLE),
      m(
        "div",
        { class: "grid gap-3 @max-[620px]:grid-cols-1 grid-cols-3" },
        options.map((option) => startTile(option, attrs)),
      ),
      footer,
    ]);
  }

  function pagedStartSomethingSection(attrs: GettingStartedPageAttrs): m.Vnode {
    const seeMore = hasMoreStartOptions(startShownCount, START_OPTIONS.length)
      ? m("div", { class: "mt-2 flex justify-end" }, [
          m(
            Button,
            {
              variant: "ghost",
              sm: true,
              extra: "getting-started-more",
              onclick: () => {
                startShownCount = nextStartCount(startShownCount, START_OPTIONS.length);
              },
            },
            SEE_MORE_LABEL,
          ),
        ])
      : null;
    return startSomethingSection(visibleStartOptions(START_OPTIONS, startShownCount), attrs, seeMore);
  }

  function templatesStatus(message: string): m.Vnode {
    return m(
      "p",
      { class: "getting-started-templates-status py-1 text-(length:--font-size-row) text-faint" },
      message,
    );
  }

  function scrollToTemplatesIfPending(section: HTMLElement): void {
    if (!isScrollToTemplatesPending) return;
    isScrollToTemplatesPending = false;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function templatesSection(catalog: TemplateCatalogState): m.Vnode | null {
    if (catalog.kind === "disabled") return null;
    let body: m.Children;
    switch (catalog.kind) {
      case "loading":
        body = templatesStatus(TEMPLATES_LOADING_MESSAGE);
        break;
      case "failed":
        body = templatesStatus(TEMPLATES_FAILED_MESSAGE);
        break;
      case "loaded":
        body = m(TemplateShelves, {
          shelves: resolveShelves(catalog.catalog),
          onPick: (template) => (detailTemplate = template),
        });
        break;
    }
    return templatesSectionShell("mt-10", m("h2", { class: SECTION_HEADING_CLASS }, TEMPLATES_TITLE), body);
  }

  /** The templates section as both the resting page and the search results draw it: the marker the template tile
   *  scrolls to, with the hooks that do the scrolling once it asked for it. */
  function templatesSectionShell(spacingClass: string, heading: m.Vnode, body: m.Children): m.Vnode {
    return m(
      "section",
      {
        "data-section": "templates",
        class: `getting-started-templates ${spacingClass}`,
        oncreate: (created: m.VnodeDOM) => scrollToTemplatesIfPending(created.dom as HTMLElement),
        onupdate: (updated: m.VnodeDOM) => scrollToTemplatesIfPending(updated.dom as HTMLElement),
      },
      [heading, body],
    );
  }

  function searchResults(attrs: GettingStartedPageAttrs): m.Children {
    const trimmed = query.trim();
    const starts = searchStartOptions(START_OPTIONS, trimmed);
    const templates = attrs.catalog.kind === "loaded" ? searchTemplates(attrs.catalog.catalog.templates, trimmed) : [];
    if (templates.length === 0) isScrollToTemplatesPending = false;
    if (starts.length === 0 && templates.length === 0) {
      return m("p", { class: "getting-started-no-matches mt-6 type-body text-secondary" }, [
        "Nothing matches “",
        m("span", { class: "text-primary" }, trimmed),
        "”.",
      ]);
    }
    return [
      starts.length === 0 ? null : startSomethingSection(starts, attrs, null),
      templates.length === 0
        ? null
        : templatesSectionShell(
            "mt-6 first:mt-0",
            m("h2", { class: `${SECTION_HEADING_CLASS} mb-2` }, SEARCH_TEMPLATES_TITLE),
            m(
              "div",
              { class: "grid gap-6 @max-[620px]:grid-cols-2 grid-cols-4" },
              templates.map((template) =>
                m(TemplateCard, {
                  key: template.slug,
                  template,
                  isFill: true,
                  onPick: (picked) => (detailTemplate = picked),
                }),
              ),
            ),
          ),
    ];
  }

  function searchField(): m.Vnode {
    return m("div", { class: "getting-started-search relative" }, [
      m(
        "span",
        { class: "pointer-events-none absolute inset-y-0 left-3 flex items-center text-faint" },
        m.trust(icon("search", { size: FIELD_GLYPH_SIZE })),
      ),
      m("input", {
        type: "text",
        "data-getting-started-search": "",
        "aria-label": SEARCH_PLACEHOLDER,
        placeholder: SEARCH_PLACEHOLDER,
        value: query,
        class: inputClass({ extra: "pl-9" }),
        oninput: (event: InputEvent) => {
          query = (event.target as HTMLInputElement).value;
        },
        onkeydown: (event: KeyboardEvent) => {
          if (event.key !== "Escape" || query === "") return;
          event.preventDefault();
          query = "";
        },
      }),
    ]);
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const body: m.Children =
        detailTemplate !== null
          ? m(TemplateDetail, {
              template: detailTemplate,
              onBack: () => {
                detailTemplate = null;
              },
              onStartWithText: attrs.onStartWithText,
            })
          : [
              searchField(),
              m(
                "div",
                { class: "mt-6" },
                query.trim() !== ""
                  ? searchResults(attrs)
                  : [pagedStartSomethingSection(attrs), templatesSection(attrs.catalog)],
              ),
            ];
      return m(
        "main",
        {
          class:
            "getting-started-page @container mx-auto min-h-screen w-full max-w-4xl bg-page px-6 py-5 text-primary",
        },
        body,
      );
    },
  };
}
