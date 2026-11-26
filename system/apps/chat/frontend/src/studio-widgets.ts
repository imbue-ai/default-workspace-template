/**
 * The widgets the chat draws inside an agent's message (specs/chat-suggestions in mngr): where the message carries
 * a widget's tag on a line of its own, the widget is drawn in the tag's place (``views/StudioPickers.ts``). Left
 * in the markdown, the tag would be removed by the sanitizer without a trace.
 */

/** A widget the chat draws in an agent's message. */
export type StudioWidgetKind = "category_picker" | "connector_picker";

/** A stretch of an agent's message: markdown, or a widget drawn in its place. */
export type ProseSegment = { kind: "markdown"; text: string } | { kind: "widget"; widget: StudioWidgetKind };

const STUDIO_WIDGET_BY_TAG: Record<string, StudioWidgetKind> = {
  studio_category_picker: "category_picker",
  studio_connector_picker: "connector_picker",
};

// A widget's tag alone on its line, self-closed or closed at once, indented less than a code block would be.
const STUDIO_WIDGET_LINE = /^ {0,3}<(studio_category_picker|studio_connector_picker)\s*(?:\/>|><\/\1>)[ \t]*$/;
const CODE_FENCE_LINE = /^ {0,3}(`{3,}|~{3,})/;

/** Whether ``source`` may carry a widget's tag: a cheap test for a caller that would otherwise split every message. */
export function mayHoldStudioWidget(source: string): boolean {
  return source.includes("<studio_");
}

/**
 * Cut an agent's message at each line that is a widget's tag, outside a code fence: the markdown around the tags
 * and the widgets between, in order. Only the two picker tags are widgets; any other tag stays in its markdown,
 * where the sanitizer treats it as before. A message with no widget is one markdown segment, unchanged.
 */
export function splitStudioWidgets(source: string): ProseSegment[] {
  if (!mayHoldStudioWidget(source)) return [{ kind: "markdown", text: source }];
  const segments: ProseSegment[] = [];
  let markdownLines: string[] = [];
  let openFence: string | null = null;
  const flushMarkdown = (): void => {
    const text = markdownLines.join("\n");
    if (text.trim() !== "") segments.push({ kind: "markdown", text });
    markdownLines = [];
  };
  for (const line of source.split("\n")) {
    const fence = CODE_FENCE_LINE.exec(line)?.[1];
    if (openFence !== null) {
      // A fence closes on a line of its own character, at least as long as the one that opened it.
      if (fence !== undefined && fence[0] === openFence[0] && fence.length >= openFence.length) openFence = null;
      markdownLines.push(line);
      continue;
    }
    if (fence !== undefined) {
      openFence = fence;
      markdownLines.push(line);
      continue;
    }
    const tag = STUDIO_WIDGET_LINE.exec(line)?.[1];
    if (tag === undefined) {
      markdownLines.push(line);
      continue;
    }
    flushMarkdown();
    segments.push({ kind: "widget", widget: STUDIO_WIDGET_BY_TAG[tag] });
  }
  flushMarkdown();
  return segments;
}
