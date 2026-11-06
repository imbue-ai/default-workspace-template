/**
 * A template's page in the store: Getting Started's detail page (its drawing, write-up and needs), plus what it
 * costs -- its download size when the store could estimate it, and a plain "not known" for memory, which a
 * template does not declare. Install hands the template to a chat that adopts it (``/use-template <url>``), exactly
 * as "Make it mine" does on Getting Started, so its trust check and its guided setup still apply.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { CatalogTemplate } from "getting-started-frontend/src/models/TemplateCatalog";
import { writeUpParagraphs } from "getting-started-frontend/src/models/TemplateCatalog";
import { adoptTemplateMessage, templateRequirements } from "getting-started-frontend/src/views/TemplateDetail";
import { TemplateArt } from "getting-started-frontend/src/views/TemplateArt";
import { formatMb } from "../room";

const SMALL_GLYPH_SIZE = 14;
const BACK_GLYPH_SIZE = 16;
const ART_FALLBACK_GLYPH_SIZE = 32;
const KB_PER_MB = 1024;

/** Where a template's size lookup stands. */
export type TemplateSize = { kind: "loading" } | { kind: "known"; sizeKb: number } | { kind: "unknown" };

export function templateSizeText(size: TemplateSize): string {
  switch (size.kind) {
    case "loading":
      return "Estimating…";
    case "unknown":
      return "Not estimated";
    case "known":
      return `about ${formatMb(size.sizeKb / KB_PER_MB)}`;
  }
}

export interface StoreTemplateDetailAttrs {
  template: CatalogTemplate;
  size: TemplateSize;
  onBack: () => void;
  onStartWithText: (text: string) => void;
}

export function StoreTemplateDetail(): m.Component<StoreTemplateDetailAttrs> {
  return {
    view(vnode) {
      const { template, size, onBack, onStartWithText } = vnode.attrs;
      const paragraphs = writeUpParagraphs(template.what_it_is);
      const requirements = templateRequirements(template);
      return m("section", { class: "app-store-template-detail", "data-template-detail": template.slug }, [
        m(Button, { variant: "ghost", sm: true, "aria-label": "Back", extra: "app-store-back", onclick: onBack }, [
          m.trust(icon("chevron-left", { size: BACK_GLYPH_SIZE })),
          m("span", "Back"),
        ]),
        m("div", { class: "mt-4 flex items-start gap-6 max-md:flex-col" }, [
          m(
            "div",
            { class: "w-full shrink-0 md:w-80" },
            m(TemplateArt, { template, frameClass: "w-full rounded-lg", glyphSize: ART_FALLBACK_GLYPH_SIZE }),
          ),
          m("div", { class: "min-w-0 flex-1" }, [
            m("h2", { class: "type-heading m-0 text-primary" }, template.title),
            template.author === ""
              ? null
              : m("p", { class: "type-helper m-0 mt-1 text-secondary" }, `by ${template.author}`),
            (paragraphs.length > 0 ? paragraphs : [template.description]).map((paragraph) =>
              m("p", { class: "type-body mt-4 text-primary" }, paragraph),
            ),
            m("section", { class: "mt-5" }, [
              m("h4", { class: "type-section m-0 text-faint" }, "Needs"),
              m(
                "ul",
                { class: "m-0 mt-2 list-none p-0", "data-needs": "" },
                requirements.length === 0
                  ? m("li", { class: "py-1 type-body text-secondary" }, "Nothing to connect or supply.")
                  : requirements.map((requirement) =>
                      m("li", { class: "flex items-baseline gap-2 py-1 type-body text-primary" }, [
                        m(
                          "span",
                          { class: "flex shrink-0 items-center self-center text-faint" },
                          m.trust(icon("key", { size: SMALL_GLYPH_SIZE })),
                        ),
                        m("span", requirement.label),
                        m("span", { class: "type-helper text-secondary" }, requirement.detail),
                      ]),
                    ),
              ),
            ]),
            m("section", { class: "mt-5" }, [
              m("h4", { class: "type-section m-0 text-faint" }, "Room"),
              m("dl", { class: "m-0 mt-2 grid grid-cols-[9rem_1fr] gap-x-4 gap-y-1.5" }, [
                m("dt", { class: "type-helper text-faint" }, "Download size"),
                m(
                  "dd",
                  { class: "m-0 type-body text-primary", "data-template-size": size.kind },
                  templateSizeText(size),
                ),
                m("dt", { class: "type-helper text-faint" }, "Memory"),
                m("dd", { class: "m-0 type-body text-secondary" }, "Not known until it's set up"),
              ]),
            ]),
            m(
              "a",
              {
                class: "mt-5 inline-flex items-center gap-1.5 type-helper text-secondary hover:text-primary",
                href: template.repository_url,
                target: "_blank",
                rel: "noopener noreferrer",
              },
              [m("span", "View the repository"), m.trust(icon("external-link", { size: SMALL_GLYPH_SIZE }))],
            ),
            m("div", { class: "mt-6 flex flex-wrap items-center gap-3" }, [
              m(
                Button,
                {
                  variant: "primary",
                  extra: "app-store-adopt",
                  onclick: () => onStartWithText(adoptTemplateMessage(template)),
                },
                "Install",
              ),
              m(
                "span",
                { class: "type-helper text-secondary" },
                "Opens a chat that checks it and walks you through setting it up.",
              ),
            ]),
          ]),
        ]),
      ]);
    },
  };
}
