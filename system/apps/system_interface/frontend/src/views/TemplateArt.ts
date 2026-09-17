/**
 * A template's drawing in the 3:2 frame every catalog surface shows it in (a card, the detail
 * dialog). The drawings are all 3:2, so the frame matches and nothing is cropped; a template with
 * no drawing, or one whose drawing fails to load, gets a quiet glyph on the page tint rather than
 * a hole in the rail or the browser's broken-image icon.
 */

import m from "mithril";
import type { CatalogTemplate } from "../models/TemplateCatalog";
import { icon } from "@imbue/workspace-ui/src/components/icons";

export interface TemplateArtAttrs {
  template: CatalogTemplate;
  // The frame's own look on top of the shared aspect, clipping, and tint: its radius, any hover lift.
  frameClass: string;
  glyphSize: number;
}

export function TemplateArt(): m.Component<TemplateArtAttrs> {
  // The slug whose drawing failed to load, so the same instance still shows another template's.
  let brokenSlug: string | null = null;

  return {
    view(vnode) {
      const { template, frameClass, glyphSize } = vnode.attrs;
      const hasArt = template.thumbnail_url !== "" && brokenSlug !== template.slug;
      return m(
        "span",
        { class: `new-tab-template-art block aspect-[3/2] overflow-hidden bg-page ${frameClass}` },
        hasArt
          ? m("img", {
              src: template.thumbnail_url,
              alt: "",
              loading: "lazy",
              class: "h-full w-full object-cover",
              onerror: () => {
                brokenSlug = template.slug;
              },
            })
          : m(
              "span",
              { class: "flex h-full w-full items-center justify-center text-faint" },
              m.trust(icon("box", { size: glyphSize })),
            ),
      );
    },
  };
}
