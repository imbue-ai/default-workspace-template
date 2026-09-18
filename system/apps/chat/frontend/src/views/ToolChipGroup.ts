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

/**
 * What a chip says about its call, in the order the reader is best served.
 *
 * A chip used to say the tool's name -- "Read", "Bash", "Grep". That is enough to
 * tell three calls apart and useless at twenty, where a row reads "Read Read Grep
 * Read Bash Read" and identifies nothing. The harness already knows better, on two
 * levels:
 *
 * 1. The agent's OWN words. Claude's shell and delegation tools require a short
 *    description of what the command is for, and the agent writes one every time
 *    ("Read the transcript container markup"). Nothing beats it, so it wins.
 * 2. Otherwise, what the call did: a past-tense verb and the thing it acted on
 *    ("read ChatPanel.ts", `searched "font-size" in src/views`). The two halves are
 *    set in different type, so they stay apart.
 *
 * The fallbacks below that are for events this app parsed before the fields
 * existed, and for the harnesses whose parsers do not stamp them yet: the live
 * strip's caption ("Reading foo.py") is the same thing in the present tense and
 * reads fine on a chip, and the bare tool name is the last resort.
 */
type ChipText = { kind: "note"; text: string } | { kind: "action"; verb: string; target: string };

export function chipText(call: ToolCall): ChipText {
  if (call.action_note) return { kind: "note", text: call.action_note };
  if (call.action_verb) return { kind: "action", verb: call.action_verb, target: call.action_target ?? "" };
  const caption = (call.caption_label ?? "").trim();
  if (caption) return { kind: "note", text: caption };
  const header = (call.header_label ?? "").replace(/^Tool:\s*/, "").trim();
  return { kind: "note", text: header || call.tool_name };
}

/** The whole phrase as one string -- the hover title, where the row's truncation
 *  does not apply and the reader wants all of it. */
function chipTitle(text: ChipText): string {
  return text.kind === "note" ? text.text : `${text.verb} ${text.target}`.trim();
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
 *  strength text once it is the open one.
 *
 *  `max-w-[20rem]`: a chip now carries a phrase rather than a word, and one long
 *  one must not take a whole line of the row to itself. Past that width the
 *  target truncates and the hover title carries the rest. */
const CHIP_BASE =
  "tool-chip inline-flex max-w-[20rem] cursor-pointer appearance-none items-center gap-1.5 rounded-md border-0 " +
  "px-2 py-[2px] text-(length:--font-size-helper) leading-normal transition-colors duration-(--dur-base) " +
  "hover:bg-fill-hover";

/** The verb is prose about what happened, so it keeps the reading face and never
 *  shrinks -- it is the short half, and clipping it would lose the sentence. */
const CHIP_VERB_CLASS = "tool-chip-verb shrink-0 whitespace-nowrap";

/** The target is the machine's own text -- a path, a pattern, a command -- so it
 *  takes the monospace face, and it is the half that gives way when a chip runs
 *  out of room. */
const CHIP_TARGET_CLASS = "tool-chip-target min-w-0 truncate font-mono opacity-80";

/** The detail panel: a bordered box with no fill of its own, so it reads as an
 *  annotation on the row rather than as another block in the transcript.
 *
 *  It is a flex item of the chip row. `basis-full` is what forces the wrap
 *  break, since an item that wants the whole width cannot share a line. `ml-2`
 *  and the matching narrower basis put it back in line with the prose, undoing
 *  the row's own `-ml-2` for this one child. */
const DETAIL_CLASS = "tool-chip-detail mt-1 mb-0.5 ml-2 basis-[calc(100%-0.5rem)] rounded-md border px-3 py-1.5";

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
    // Keyed because its siblings in the row are: mithril rejects a fragment
    // that mixes keyed and unkeyed children.
    { class: DETAIL_CLASS, key: `detail-${chip.call.tool_call_id}` },
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

    // The panel goes INSIDE the row, immediately after the chip that opened it.
    // A long run wraps onto several lines, and a panel hung below the whole row
    // ends up lines away from the chip it belongs to -- with unrelated chips in
    // between, so the reader loses which one they opened. As a full-width flex
    // item it cannot share a line, which breaks the wrap exactly where it sits:
    // the chip it belongs to ends its line, the panel spans the width directly
    // underneath, and the rest of the run resumes below it.
    return m("div", { class: "tool-chip-group my-1.5" }, [
      m(
        "div",
        { class: ROW_CLASS },
        chips.flatMap((chip) => {
          const isOpen = open !== null && open.call.tool_call_id === chip.call.tool_call_id;
          const failed = toolResults.get(chip.call.tool_call_id)?.is_error === true;
          const text = chipText(chip.call);
          // Colour says two different things at once, so they are ordered: a
          // failed call stays red whether or not it is the open one, since the
          // failure matters more than the selection.
          const tone = failed ? "text-danger" : isOpen ? "text-primary" : "text-faint";
          const fill = isOpen ? "tool-chip--selected bg-fill-active" : "bg-transparent";
          const button = m(
            "button",
            {
              type: "button",
              class: `${CHIP_BASE} ${tone} ${fill}`,
              "aria-pressed": isOpen ? "true" : "false",
              // The untruncated phrase, plus the tool it came from -- which the
              // chip itself no longer says anywhere.
              title: `${chipTitle(text)}\n${chip.call.tool_name}`,
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
              text.kind === "note"
                ? m("span", { class: "tool-chip-label min-w-0 truncate" }, text.text)
                : [
                    m("span", { class: `tool-chip-label ${CHIP_VERB_CLASS}` }, text.verb),
                    text.target ? m("span", { class: CHIP_TARGET_CLASS }, text.target) : null,
                  ],
            ],
          );
          if (!isOpen) return [button];
          return [button, renderDetail(chip, toolResults.get(chip.call.tool_call_id) ?? null, chatId)];
        }),
      ),
    ]);
  },
};
