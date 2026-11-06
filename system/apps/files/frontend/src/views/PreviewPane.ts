/**
 * A file at a glance, as the last of the columns: the picture, player, or first lines of text it holds, what it is,
 * and the ways to open it or take a copy.
 */

import m from "mithril";

import { Button } from "@imbue/workspace-ui/src/components/Button";

import { readText, urlOf, type Entry } from "../models/server";
import { formatModified, formatSize } from "../models/format";
import { isTextLike, kindLabelOf, previewKindOf } from "../models/fileKinds";
import { FileIcon } from "./FileIcon";
import type { FilesController } from "./FilesController";

/** How much of a text file the preview reads: enough for a screenful, never the whole of a big log. */
const SNIPPET_BYTES = 4096;

interface Snippet {
  key: string;
  text: string | null;
}

export function PreviewPane(): m.Component<{ app: FilesController; entry: Entry }> {
  let snippet: Snippet | null = null;
  let request: AbortController | null = null;

  const loadSnippet = (entry: Entry): void => {
    const key = `${entry.path}@${entry.mtime}`;
    if (snippet?.key === key) return;
    request?.abort();
    snippet = { key, text: null };
    if (!isTextLike(entry.name) || entry.size === 0) return;
    const controller = new AbortController();
    request = controller;
    // A small file is read whole, so no range runs past its end.
    readText(entry.path, controller.signal, entry.size > SNIPPET_BYTES ? SNIPPET_BYTES : undefined)
      .then((text) => {
        if (snippet?.key === key) snippet = { key, text };
        m.redraw();
      })
      .catch(() => undefined);
  };

  return {
    onremove() {
      request?.abort();
    },
    view({ attrs: { app, entry } }) {
      const preview = previewKindOf(entry.name, false);
      const url = `${urlOf(entry.path)}?t=${entry.mtime}`;
      loadSnippet(entry);
      let picture: m.Children;
      if (preview === "image") {
        picture = m("img.files-preview-image", {
          src: url,
          alt: "",
          class: "max-h-full max-w-full object-contain",
        });
      } else if (preview === "video") {
        picture = m("video", {
          src: url,
          controls: true,
          preload: "metadata",
          class: "max-h-full w-full rounded-md",
        });
      } else if (preview === "audio") {
        picture = [m(FileIcon, { entry, size: 112 }), m("audio", { src: url, controls: true, class: "w-full" })];
      } else if (snippet?.text !== null && snippet?.text !== undefined) {
        picture = m(
          "pre.files-preview-text",
          {
            class:
              "h-full w-full overflow-hidden rounded-md border border-subtle bg-surface p-3 font-mono text-(length:--font-size-helper) leading-snug whitespace-pre-wrap break-words text-secondary shadow-raised",
          },
          snippet.text,
        );
      } else {
        picture = m(FileIcon, { entry, size: 128 });
      }
      return m("div.files-preview", { class: "flex flex-col items-center gap-3 p-4" }, [
        // One fixed box for whatever stands for the file, so the pane does not jump as an icon gives way to a file's
        // text, or from one picture's shape to the next.
        m(
          "div.files-preview-picture",
          { class: "flex h-[240px] w-full flex-col items-center justify-center gap-3" },
          picture,
        ),
        m("div", { class: "w-full text-center type-label break-words text-primary" }, entry.name),
        m(
          "div",
          { class: "type-helper text-secondary" },
          `${kindLabelOf(entry.name, false)} · ${formatSize(entry.size)}`,
        ),
        m("dl", { class: "grid w-full grid-cols-[auto_1fr] gap-x-3 gap-y-1 type-helper" }, [
          m("dt", { class: "text-faint" }, "Modified"),
          m("dd", { class: "m-0 text-right text-secondary" }, formatModified(entry.mtime)),
        ]),
        m("div", { class: "flex w-full gap-2" }, [
          m(
            Button,
            { sm: true, extra: "flex-1", "data-preview-action": "open", onclick: () => app.model.open(entry) },
            "Open",
          ),
          m(
            Button,
            { sm: true, extra: "flex-1", "data-preview-action": "download", onclick: () => app.download([entry]) },
            "Download",
          ),
        ]),
      ]);
    },
  };
}
