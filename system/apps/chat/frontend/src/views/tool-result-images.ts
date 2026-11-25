/**
 * The images a tool call's output carries, each served by its result's images route.
 *
 * Images the agent generated are the point of the call, so the transcript walk lifts them out of the call's chip
 * and out of any step (the `generated_images` timeline item) and they show with no click. Any other image (one the
 * agent only looked at) is detail of the call, shown in its chip's panel under the text output.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { openImageLightbox } from "../lightbox";
import type { AssistantMessageEvent, ToolResultEvent } from "../models/Response";
import { railNodeAttrs } from "./agent-rail";
import { STRETCH_PIECES_CLASS } from "./machine-bubbles";

function imageCount(result: ToolResultEvent): number {
  return result.image_count ?? 0;
}

/** Whether the result carries images that image generation made. */
export function hasGeneratedImages(result: ToolResultEvent): boolean {
  return result.is_generated_image === true && imageCount(result) > 0;
}

/** Whether the result carries images that belong in its chip's panel: any it carries that were not generated. */
export function hasOutputImages(result: ToolResultEvent): boolean {
  return result.is_generated_image !== true && imageCount(result) > 0;
}

function toolResultImageUrl(chatId: string, resultEventId: string, index: number): string {
  return apiUrl(
    `/api/chats/${encodeURIComponent(chatId)}/events/${encodeURIComponent(resultEventId)}/images/${index}`,
  );
}

/** The images that have loaded on this page, so one the virtualized list mounts again shows at once rather than
 *  holding its loading box until the cached copy decodes. */
const loadedUrls = new Set<string>();

interface ToolResultImageAttrs {
  url: string;
  alt: string;
}

const IMAGE_CLASS = "tool-result-image block h-auto max-h-96 w-auto max-w-full rounded-lg object-contain";

/** One image: a box that keeps its space while it loads, a note in its place when it cannot be fetched, and a click
 *  that opens it full-screen. */
function ToolResultImage(): m.Component<ToolResultImageAttrs> {
  let phase: "loading" | "shown" | "failed" | null = null;
  return {
    view(vnode) {
      const { url, alt } = vnode.attrs;
      phase ??= loadedUrls.has(url) ? "shown" : "loading";
      if (phase === "failed") {
        return m(
          "div",
          {
            class:
              "tool-result-image-error rounded-lg border border-dashed px-3 py-2 " +
              "text-(length:--font-size-helper) italic text-secondary",
          },
          "Couldn't load this image.",
        );
      }
      return m(
        "button",
        {
          type: "button",
          class: "tool-result-image-open block max-w-full cursor-zoom-in appearance-none border-0 bg-transparent p-0",
          "aria-label": `Open ${alt}`,
          onclick: () => openImageLightbox(url, alt, "the image"),
        },
        m("img", {
          class: phase === "loading" ? `${IMAGE_CLASS} is-loading` : IMAGE_CLASS,
          src: url,
          alt,
          loading: "lazy",
          decoding: "async",
          onload: () => {
            loadedUrls.add(url);
            phase = "shown";
          },
          onerror: () => {
            phase = "failed";
          },
        }),
      );
    },
  };
}

/** Every image a result carries, in order. `description` is each one's alt text (numbered when there are several). */
export function renderToolResultImages(chatId: string, result: ToolResultEvent, description: string): m.Vnode {
  const count = imageCount(result);
  return m(
    "div",
    { class: "tool-result-images flex flex-wrap items-start gap-2" },
    Array.from({ length: count }, (_, index) =>
      m(ToolResultImage, {
        key: index,
        url: toolResultImageUrl(chatId, result.event_id, index),
        alt: count === 1 ? description : `${description} (${index + 1} of ${count})`,
      }),
    ),
  );
}

/** The `generated_images` timeline item as a part of a stretch: the images each result carries, on the line as a
 *  box, right after the message whose calls made them. */
export function renderGeneratedImagesItem(
  event: AssistantMessageEvent,
  results: readonly ToolResultEvent[],
  chatId: string,
): m.Vnode {
  return m(
    "div",
    { class: `generated-images ${STRETCH_PIECES_CLASS}`, key: `images-${event.event_id}` },
    results.map((result) =>
      m(
        "div",
        { key: result.event_id, ...railNodeAttrs("box") },
        renderToolResultImages(chatId, result, "Generated image"),
      ),
    ),
  );
}
