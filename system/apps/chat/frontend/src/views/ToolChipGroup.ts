/**
 * A run of tool calls as a row of inline chips.
 *
 * The run collapses into one wrapping row of small ghost chips that reads as a
 * single line of what the agent did, and only the chip a reader picks opens its
 * detail below the row. One open at a time: the row is a list to scan.
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
 * What a chip says about its call, in the order the reader is best served. The
 * tool's own name would tell one call from another only while there are few of
 * them, so the harness's two better answers come first:
 *
 * 1. The agent's OWN words. Claude's shell and delegation tools require a short
 *    description of what the command is for, and the agent writes one every time
 *    ("Read the transcript container markup"). Nothing beats it, so it wins.
 * 2. Otherwise, what the call did: a past-tense verb and the thing it acted on
 *    ("read ChatPanel.ts", `searched "font-size" in views`). A file is named, not
 *    pathed -- the parser shortens it, and the whole path is in the panel.
 *
 * The fallbacks below that cover an event parsed before the fields existed, and
 * a harness whose parser stamps no action: the live strip's caption ("Reading
 * foo.py") is the same thing in the present tense and reads fine on a chip, and
 * the bare tool name is the last resort.
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

/** The group's own margins separate it from its SIBLINGS inside a message, and
 *  they are deliberately NOT equal. Above it is the line that introduces the
 *  run ("Checking how much disk..."), which the run belongs to: tight, so it
 *  reads as that sentence's work. Below it the agent has moved on to what it
 *  found, which is a new message however the harness packaged it: the full
 *  message gap, so it reads as one.
 *
 *  At the edges of a message they would instead add to the message's own
 *  margin, making that seam wider than every other seam in the transcript -- so
 *  there they collapse and the message rhythm alone does the spacing, the same
 *  shape as the `p:last-child` rules the markdown blocks carry. */
const GROUP_CLASS = "tool-chip-group mt-1.5 mb-5 first:mt-0 last:mb-0";

/** `-ml-1` cancels the first chip's own left padding, so the row's ink starts
 *  where the prose above it does: a ghost button needs that padding for its
 *  hover fill to have a shape, but the padding is chrome. It tracks the chip's
 *  `px-1`, as does the panel's own margin below. */
const ROW_CLASS = "tool-chip-row -ml-1 flex flex-wrap items-center gap-1";

/** A ghost button: no fill at rest, a wash on hover, a stronger fill and full-
 *  strength text once it is the open one.
 *
 *  `max-w-[20rem]`: a chip carries a phrase, and one long one must not take a
 *  whole line of the row to itself. Past that width the target truncates and the
 *  hover title carries the rest. */
const CHIP_BASE =
  "tool-chip inline-flex max-w-[20rem] cursor-pointer appearance-none items-center gap-0.5 rounded-md border-0 " +
  "px-1 py-[2px] text-(length:--font-size-helper) leading-normal transition-colors duration-(--dur-base) " +
  "hover:bg-fill-hover";

/**
 * The two halves of a chip's phrase read as ONE sentence: same face, same size,
 * same colour, separated by an ordinary word space. A chip is a thing to read,
 * so it is set like reading rather than like the console output it describes.
 *
 * They stay separate elements because they are separate facts, which the tests
 * and the e2e suite locate individually. Both are bare markers: the wrapping
 * label owns the layout, so the space between them is a real text node rather
 * than the row's flex gap, which at this size reads as a double space.
 */
const CHIP_LABEL_CLASS = "tool-chip-label min-w-0 truncate";

/** The detail panel: a bordered box with no fill of its own, so it reads as an
 *  annotation on the row rather than as another block in the transcript.
 *
 *  It is a flex item of the chip row. `basis-full` is what forces the wrap
 *  break, since an item that wants the whole width cannot share a line. `ml-1`
 *  and the matching narrower basis put it back in line with the prose, undoing
 *  the row's own `-ml-1` for this one child. */
const DETAIL_CLASS = "tool-chip-detail mt-1 mb-0.5 ml-1 basis-[calc(100%-0.25rem)] rounded-md border px-3 py-1.5";

/** The panel's header: which call this is, and the way out of it.
 *
 *  What it is for is room. A chip caps its phrase at 20rem and truncates, so the
 *  full text of what a call did had nowhere to be said; here it has the panel's
 *  whole width, and wraps rather than truncating when even that is not enough.
 *  `items-start` keeps the glyph and the close control on the first line when it
 *  does.
 *
 *  The negative margins undo the panel's own padding, so the header spans its full
 *  width and meets its rounded top corners. */
const DETAIL_HEADER_CLASS =
  "tool-chip-detail-header -mx-3 -mt-1.5 mb-1.5 flex items-start gap-1.5 " +
  "rounded-t-md border-b px-3 py-1.5 text-(length:--font-size-helper) leading-normal";

/** The close control: the same ghost treatment as a chip, squared off for an icon.
 *  `-mr-2` sets it nearer the panel's edge than its own padding would -- the gap that
 *  reads as right is the one to the glyph, not to the invisible box around it. */
const DETAIL_CLOSE_CLASS =
  "tool-chip-detail-close -mr-2 ml-auto flex shrink-0 cursor-pointer appearance-none items-center rounded " +
  "border-0 bg-transparent p-1 text-faint transition-colors duration-(--dur-base) hover:bg-fill-hover " +
  "hover:text-primary";

/** The code itself adds only how it wraps; the pane around it sets the face. */
const PANE_CODE_CLASS = "break-all whitespace-pre-wrap";

/** The pane itself carries the face and size, so everything in it -- the verb,
 *  the command, and the space BETWEEN them -- is set alike. The space is an
 *  ordinary text node, and a text node takes the face of whatever contains it,
 *  so on the pane's default face it would be a sans space beside monospace. */
const PANE_CLASS = "py-0.5 font-mono text-(length:--font-size-helper) leading-normal";

function renderPane(marker: string, text: string, extra = "", verb?: string): m.Vnode {
  return m("div", { class: `${marker} ${PANE_CLASS} ${extra}`.trim() }, [
    // Beside the command rather than above it, so a one-line input stays one
    // line, and in the command's own face so the two read as one line rather
    // than as a label stuck to a value.
    verb ? m("span", { class: "tool-call-verb text-secondary" }, verb) : null,
    verb ? " " : null,
    m(
      "pre",
      { class: `m-0 overflow-x-auto border-0 bg-transparent p-0${verb ? " inline align-top" : ""}` },
      m("code", { class: PANE_CODE_CLASS }, text),
    ),
  ]);
}

/** Whether `value` is the field the chip's note was made from. The note is not
 *  the raw field: the backend runs it through a whitespace-collapse and an
 *  80-char clip (see `stated_note`/`shorten`), so a raw description with a
 *  newline, a doubled space, or more than 80 characters no longer equals its
 *  note. Matching the same normalization -- and, for a clipped note (one that
 *  ends in the ellipsis), its leading run -- drops the field the note came from
 *  rather than leaving it to print a second time under the chip. */
function noteEchoesValue(value: string, note: string): boolean {
  const collapsed = value.replace(/\s+/g, " ").trim();
  if (collapsed === note) return true;
  return note.endsWith("…") && collapsed.startsWith(note.slice(0, -1));
}

/**
 * A tool's input as something a person reads, rather than the raw JSON object it
 * arrives as.
 *
 * The object's braces and quoting are the wire's, not the reader's. A lone
 * remaining field renders as its bare value -- which for a shell call is exactly
 * the command -- and several render as `key: value` lines, with a multi-line
 * value dropped below its key rather than run onto it.
 *
 * `omit` drops what the panel has already said. The chip is the agent's own note,
 * and that note IS one of these fields (a shell call's `description`), so leaving
 * it in would print it twice. Matched by (normalized) value, so it only ever
 * drops the field the note actually came from.
 *
 * An input that is not a JSON object at all -- codex's code-mode program, a bare
 * string -- is shown verbatim; there is nothing to unpack.
 */
export function formatToolInput(raw: string, omit?: string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return raw;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return raw;

  const entries = Object.entries(parsed as Record<string, unknown>)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .filter(
      ([, value]) => !(omit !== undefined && omit !== "" && typeof value === "string" && noteEchoesValue(value, omit)),
    )
    .map(([key, value]) => [key, typeof value === "string" ? value : JSON.stringify(value)] as const);

  if (entries.length === 0) return "";
  if (entries.length === 1) return entries[0][1];
  return entries.map(([key, value]) => (value.includes("\n") ? `${key}:\n${value}` : `${key}: ${value}`)).join("\n");
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

/** How much of a long output the panel shows before asking. Enough that most calls
 *  are unaffected and a long one still opens with its shape visible, short enough
 *  that a thousand-line log does not bury the rest of the transcript when a chip is
 *  opened by mistake.
 *
 *  Both limits are needed. Lines alone miss the payload with no newlines in it at
 *  all -- minified JSON, base64, a captured request body -- which the pane wraps
 *  (`break-all whitespace-pre-wrap`) into hundreds of visual lines from one logical
 *  one. Characters alone would clamp mid-line on ordinary console output. */
const OUTPUT_CLAMP_LINES = 20;
const OUTPUT_CLAMP_CHARS = 2000;

/** Where the "showing all of it" state lives. Its own key rather than the chip's,
 *  so unfolding a log does not also count as opening the chip -- and cleared with
 *  the chip (see {@link closeChip}), so reopening one starts clamped again. */
function outputKey(call: ToolCall): string {
  return `chip-output:${call.tool_call_id}`;
}

/** Close a chip. The unfolded-output state is a second key, and the expansion store
 *  is never swept, so without clearing it here "showing all" would outlive the panel
 *  it belongs to and a reopened chip would dump its whole log again. */
function closeChip(call: ToolCall): void {
  setBlockExpanded(chipKey(call), false);
  setBlockExpanded(outputKey(call), false);
}

/** A text button under the clamped pane; sized and toned like a chip, since it is
 *  the same kind of thing (a small control the reader may ignore). `-ml-1` cancels
 *  its own padding so its ink starts on the pane's, as the chip row's does. */
const OUTPUT_TOGGLE_CLASS =
  "tool-call-output-toggle -ml-1 mt-1 cursor-pointer appearance-none rounded border-0 bg-transparent px-1 " +
  "py-[2px] text-(length:--font-size-helper) leading-normal text-secondary transition-colors " +
  "duration-(--dur-base) hover:bg-fill-hover hover:text-primary";

/** The lines of an output, without the empty one a trailing newline leaves behind --
 *  otherwise a 20-line log that ends in a newline reports itself as 21. */
function outputLines(text: string): string[] {
  const lines = text.split("\n");
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** What to show of an output and what to call the rest, or null when the whole of
 *  it is short enough to show. */
function clampedOutput(text: string): { shown: string; label: string } | null {
  const lines = outputLines(text);
  const byLines = lines.length > OUTPUT_CLAMP_LINES;
  let shown = byLines ? lines.slice(0, OUTPUT_CLAMP_LINES).join("\n") : text;
  const byChars = shown.length > OUTPUT_CLAMP_CHARS;
  if (byChars) shown = shown.slice(0, OUTPUT_CLAMP_CHARS);
  if (!byLines && !byChars) return null;
  // Counting lines is the useful measure when there are lines to count; for one
  // long line it would read "View all 1 lines" and promise nothing.
  return { shown, label: byLines ? `View all ${lines.length} lines` : "Show the whole output" };
}

/** The output pane, clamped when it is long enough to be worth asking about. */
function renderOutput(call: ToolCall, text: string): m.Vnode {
  const clamped = clampedOutput(text);
  if (clamped === null) return renderPane("tool-call-output", text);
  const showingAll = isBlockExpanded(outputKey(call));
  return m("div", [
    renderPane("tool-call-output", showingAll ? text : clamped.shown),
    m(
      "button",
      {
        type: "button",
        class: OUTPUT_TOGGLE_CLASS,
        onclick: () => setBlockExpanded(outputKey(call), !showingAll),
      },
      showingAll ? "Show less" : clamped.label,
    ),
  ]);
}

/** The panel's own title bar: which call this is, and the way out of it. */
function renderDetailHeader(chip: ChipCall): m.Vnode {
  return m("div", { class: DETAIL_HEADER_CLASS }, [
    m.trust(
      // `mt-[3px]` centres a 13px glyph on the first line of a ~19px line box, which
      // matters only once the title wraps and `items-start` stops doing it.
      icon(toolIcon(chip.call.tool_name), {
        size: 13,
        strokeWidth: 1.75,
        className: "tool-chip-icon mt-[3px] shrink-0",
      }),
    ),
    // The whole phrase, wrapping if it must -- the chip above truncated it, and this
    // is the one place with the width to say it in full.
    m(
      "span",
      { class: "tool-chip-detail-title min-w-0 text-secondary", title: chip.call.tool_name },
      chipTitle(chipText(chip.call)),
    ),
    m(
      "button",
      {
        type: "button",
        class: DETAIL_CLOSE_CLASS,
        "aria-label": "Close",
        title: "Close",
        onclick: () => closeChip(chip.call),
      },
      m.trust(icon("close", { size: 14, strokeWidth: 1.75 })),
    ),
  ]);
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
    sections.push(renderPane("tool-call-error-snippet", toolResult.error_snippet));
  }
  if (inputState === "loaded") {
    const input = formatToolInput(inputText, chip.call.action_note);
    // The verb leads the input only when the chip is showing the agent's note
    // instead: there it is the missing half, turning the pane into "ran <the
    // command>". When the chip already reads "edited <file>", the verb here
    // would say it twice.
    const verb = chip.call.action_note ? chip.call.action_verb : undefined;
    if (input) sections.push(renderPane("tool-call-input", input, "", verb));
  } else {
    sections.push(renderPaneNote("tool-call-input", inputState));
  }
  if (outputState === "loaded") {
    if (outputText) sections.push(renderOutput(chip.call, outputText));
  } else {
    sections.push(renderPaneNote("tool-call-output", outputState));
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
    [
      renderDetailHeader(chip),
      // Dashed: the rule between the panes separates two parts of one call, which is
      // a lighter claim than the solid one under the header (that divides the panel's
      // chrome from its contents).
      ...sections.map((section, i) =>
        i === 0 ? section : m("div", { class: "mt-1.5 border-t border-dashed pt-1.5" }, section),
      ),
    ],
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

    // The panel goes INSIDE the row, immediately after the chip that opened it,
    // because a long run wraps and a panel hung below the whole row would sit
    // lines away from its own chip. As a full-width flex item it cannot share a
    // line, which breaks the wrap exactly where it sits: the chip that opened it
    // ends its line, the panel spans the width underneath, and the rest of the
    // run resumes below.
    return m("div", { class: GROUP_CLASS }, [
      m(
        "div",
        { class: ROW_CLASS },
        chips.flatMap((chip) => {
          const isOpen = open !== null && open.call.tool_call_id === chip.call.tool_call_id;
          const text = chipText(chip.call);
          // A failed call is NOT marked here: the agent says what it means, in
          // prose, a line or two below, which is the form a reader can act on.
          // Colour is left to say the one thing nothing else does -- which chip
          // is open.
          const tone = isOpen ? "text-primary" : "text-faint";
          const fill = isOpen ? "tool-chip--selected bg-fill-active" : "bg-transparent";
          const button = m(
            "button",
            {
              type: "button",
              class: `${CHIP_BASE} ${tone} ${fill}`,
              "aria-pressed": isOpen ? "true" : "false",
              // The untruncated phrase, plus the tool it came from, which the
              // chip itself does not say anywhere.
              title: `${chipTitle(text)}\n${chip.call.tool_name}`,
              key: chip.call.tool_call_id,
              onclick: () => {
                // One open at a time: opening a chip closes whichever was open.
                if (open !== null) closeChip(open.call);
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
                ? m("span", { class: CHIP_LABEL_CLASS }, text.text)
                : m("span", { class: CHIP_LABEL_CLASS }, [
                    m("span", { class: "tool-chip-verb" }, text.verb),
                    text.target ? " " : null,
                    text.target ? m("span", { class: "tool-chip-target" }, text.target) : null,
                  ]),
            ],
          );
          if (!isOpen) return [button];
          return [button, renderDetail(chip, toolResults.get(chip.call.tool_call_id) ?? null, chatId)];
        }),
      ),
    ]);
  },
};
