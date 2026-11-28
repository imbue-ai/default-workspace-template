/**
 * A listing's picture in the 3:2 frame every catalog surface shows it in (a card, the detail
 * page). A template's drawing is 3:2, so it fills the frame and nothing is cropped; an app has
 * only its square icon, which sits whole in the middle of the same frame so a rail of both stays
 * level. A listing with no picture, or one whose picture fails to load, gets a quiet glyph on the
 * page tint rather than a hole in the rail or the browser's broken-image icon.
 */

import m from "mithril";
import type { CatalogListing } from "../models/TemplateCatalog";
import { listingKey } from "../models/TemplateCatalog";
import { icon } from "@imbue/workspace-ui/src/components/icons";

export interface TemplateArtAttrs {
  listing: CatalogListing;
  // The frame's own look on top of the shared aspect, clipping, and tint: its radius, any hover lift.
  frameClass: string;
  glyphSize: number;
}

/** Where a listing's picture is: a template's drawing or an app's icon; "" when it has none. */
function listingArtUrl(listing: CatalogListing): string {
  return listing.kind === "template" ? listing.template.thumbnail_url : listing.app.icon_url;
}

export function TemplateArt(): m.Component<TemplateArtAttrs> {
  // The listing whose picture failed to load, so the same instance still shows another listing's.
  let brokenKey: string | null = null;

  return {
    view(vnode) {
      const { listing, frameClass, glyphSize } = vnode.attrs;
      const key = listingKey(listing);
      const artUrl = listingArtUrl(listing);
      const hasArt = artUrl !== "" && brokenKey !== key;
      return m(
        "span",
        { class: `new-tab-template-art block aspect-[3/2] overflow-hidden bg-page ${frameClass}` },
        hasArt
          ? m("img", {
              src: artUrl,
              alt: "",
              loading: "lazy",
              class:
                listing.kind === "template" ? "h-full w-full object-cover" : "h-full w-full object-contain p-[12%]",
              onerror: () => {
                brokenKey = key;
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
