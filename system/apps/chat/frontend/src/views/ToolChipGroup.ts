/**
 * A run of tool calls as a row of inline chips.
 *
 * A turn's actions used to be a stack of full-width bordered blocks, one per
 * call, each as loud as the prose around it -- so a turn that read three files
 * looked like three paragraphs of content. Here the run collapses into one
 * wrapping row of small ghost chips (glyph + name) that reads as a single line
 * of "what it did", and only the chip a reader picks opens its detail below the
 * row. One open at a time: the row is a list to scan, not a set of boxes to
 * leave lying open.
 *
 * Runs merge across assistant events, so the chips carry the id of the event
 * each call came from -- that event is where the call's input is fetched from
 * (see tool-payloads).
 */

import m from "mithril";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import { icon, type IconName } from "@imbue/workspace-ui/src/components/icons";
import { isBlockExpanded, setBlockExpanded } from "./expansion-state";
import { resolveToolPayloads } from "./tool-payloads";

/** One call in a run, with the assistant event that issued it. */
export interface ChipCall {
  call: ToolCall;
  /** The issuing assistant message's event_id -- the handle its input is fetched by. */
  eventId: string;
}

/** The glyph for a tool. The names an agent's tools go by are the harness's, so
 *  this maps the ones we see; anything unrecognised gets the wrench rather than
 *  nothing, so an unfamiliar tool still reads as a tool. */
function toolIcon(toolName: string): IconName {
  switch (toolName) {
    case "Read":
    case "NotebookRead":
      return "file";
    case "Bash":
    case "BashOutput":
      return "terminal";
    case "Edit":
    case "Write":
    case "NotebookEdit":
      return "edit";
    case "Skill":
      return "sparkle";
    case "Grep":
    case "Glob":
      return "search";
    case "Agent":
    case "Task":
      return "bot";
    case "WebFetch":
    case "WebSearch":
      return "globe";
    default:
      return "wrench";
  }
}

/** What the chip says. The harness already worked out a human label for the
 *  call -- for codex that means unwrapping an `exec` whose real operation is
 *  buried in a JS argument -- and claude's is "Tool: Read", whose prefix is
 *  noise once the glyph beside it says "tool". */
export function chipLabel(call: ToolCall): string {
  const label = (call.header_label ?? "").replace(/^Tool:\s*/, "").trim();
  return label || call.tool_name;
}

/** Where a chip's open/closed state lives. Keyed by the call, so it survives
 *  the row unmounting and remounting (virtualization) or re-rendering
 *  (streaming) -- the same store the step bodies and blocks use. */
function chipKey(call: ToolCall): string {
  return `chip:${call.tool_call_id}`;
}

/** `-ml-2` cancels the first chip's own left padding, so the row's ink starts
 *  where the prose above it does. A ghost button needs that padding for its
 *  hover fill to have a shape, but the padding is chrome -- without this the
 *  whole row sat indented from the text it belongs to. */
const ROW_CLASS = "tool-chip-row -ml-2 flex flex-wrap items-center gap-1";

/** A ghost button: no fill at rest, a wash on hover, a stronger fill and full-
 *  strength text once it is the open one. */
const CHIP_BASE =
  "tool-chip inline-flex cursor-pointer appearance-none items-center gap-1.5 rounded-md border-0 px-2 py-[2px] " +
  "text-(length:--font-size-helper) leading-normal transition-colors duration-(--dur-base) hover:bg-fill-hover";

/** The detail panel: a bordered box with no fill of its own, so it reads as an
 *  annotation on the row rather than as another block in the transcript. */
const DETAIL_CLASS = "tool-chip-detail mt-1 rounded-md border px-3 py-1.5";

const PANE_CODE_CLASS = "font-mono text-(length:--font-size-helper) leading-normal break-all whitespace-pre-wrap";

function renderPane(marker: string, text: string, extra = ""): m.Vnode {
  return m(
    "div",
    { class: `${marker} py-0.5 ${extra}`.trim() },
    m(
      "pre",
      { class: "m-0 overflow-x-auto border-0 bg-transparent p-0" },
      m("code", { class: PANE_CODE_CLASS }, text),
    ),
  );
}

/** A pane still fetching, or one whose payload the backend no longer holds. */
function renderPaneNote(marker: string, state: "loading" | "unavailable", extra = ""): m.Vnode {
  return m(
    "div",
    {
      class:
        `${marker} tool-call-payload-note py-0.5 text-(length:--font-size-helper) italic text-secondary ${extra}`.trim(),
    },
    state === "loading" ? "Loading…" : "No longer available",
  );
}

function renderDetail(chip: ChipCall, toolResult: ToolResultEvent | null, chatId: string): m.Vnode {
  const { inputText, inputState, outputText, outputState, requestPayloads } = resolveToolPayloads(
    chip.call,
    toolResult,
    chatId,
    chip.eventId,
  );
  // Re-requested on every open render: a no-op when cached or in flight, and it
  // heals an entry dropped by a transient fetch failure.
  requestPayloads();

  const isError = toolResult?.is_error === true;
  const sections: m.Vnode[] = [];

  // A failed call leads with what went wrong. The snippet rides the event, so
  // it is readable the instant the panel opens, while the full output -- which
  // is what actually has to be fetched -- is still on its way.
  if (isError && toolResult?.error_snippet) {
    sections.push(renderPane("tool-call-error-snippet", toolResult.error_snippet, "text-danger"));
  }
  if (inputState === "loaded") {
    if (inputText) sections.push(renderPane("tool-call-input", inputText));
  } else {
    sections.push(renderPaneNote("tool-call-input", inputState));
  }
  const outputMarker = isError ? "tool-call-output tool-call-output--error" : "tool-call-output";
  if (outputState === "loaded") {
    if (outputText) sections.push(renderPane(outputMarker, outputText, isError ? "text-danger" : ""));
  } else {
    sections.push(renderPaneNote(outputMarker, outputState));
  }

  // A call with nothing recorded either way still says so: an empty box would
  // read as the panel having failed to open.
  if (sections.length === 0) {
    sections.push(
      m(
        "div",
        { class: "tool-call-payload-note py-0.5 text-(length:--font-size-helper) italic text-secondary" },
        "Nothing was recorded for this call.",
      ),
    );
  }

  // A hairline between whatever sections there turned out to be, rather than a
  // rule pinned to one pair: which of the three exist depends on the call.
  return m(
    "div",
    { class: DETAIL_CLASS },
    sections.map((section, i) => (i === 0 ? section : m("div", { class: "mt-1.5 border-t pt-1.5" }, section))),
  );
}

interface ToolChipGroupAttrs {
  chips: ChipCall[];
  toolResults: Map<string, ToolResultEvent>;
  chatId: string;
}

export const ToolChipGroup: m.Component<ToolChipGroupAttrs> = {
  view(vnode) {
    const { chips, toolResults, chatId } = vnode.attrs;
    const open = chips.find((chip) => isBlockExpanded(chipKey(chip.call))) ?? null;

    return m("div", { class: "tool-chip-group my-1.5" }, [
      m(
        "div",
        { class: ROW_CLASS },
        chips.map((chip) => {
          const isOpen = open !== null && open.call.tool_call_id === chip.call.tool_call_id;
          const failed = toolResults.get(chip.call.tool_call_id)?.is_error === true;
          // Colour says two different things at once, so they are ordered: a
          // failed call stays red whether or not it is the open one, since the
          // failure matters more than the selection.
          const tone = failed ? "text-danger" : isOpen ? "text-primary" : "text-faint";
          const fill = isOpen ? "tool-chip--selected bg-fill-active" : "bg-transparent";
          return m(
            "button",
            {
              type: "button",
              class: `${CHIP_BASE} ${tone} ${fill}`,
              "aria-pressed": isOpen ? "true" : "false",
              title: chip.call.tool_name,
              key: chip.call.tool_call_id,
              onclick: () => {
                // One open at a time: opening a chip closes whichever was open.
                if (open !== null) setBlockExpanded(chipKey(open.call), false);
                if (!isOpen) setBlockExpanded(chipKey(chip.call), true);
              },
            },
            [
              m.trust(
                icon(toolIcon(chip.call.tool_name), {
                  size: 13,
                  strokeWidth: 1.75,
                  className: "tool-chip-icon shrink-0",
                }),
              ),
              m("span", { class: "tool-chip-label whitespace-nowrap" }, chipLabel(chip.call)),
            ],
          );
        }),
      ),
      open === null ? null : renderDetail(open, toolResults.get(open.call.tool_call_id) ?? null, chatId),
    ]);
  },
};
