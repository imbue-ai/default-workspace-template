/**
 * TOOL_RUNNING caption registry: maps an agent's harness to its caption peer.
 *
 * Each harness contributes one module here (``claude`` / ``codex``), exporting a
 * pure ``(ToolCall) -> string`` labeller. Callers route through ``toolLabelFor``
 * rather than branching on the harness themselves, so adding a harness is a new
 * module plus one entry in the table below -- no edits to the views.
 */

import type { ToolCall } from "../../models/Response";
import { claudeToolLabel } from "./claude";
import { codexToolLabel } from "./codex";

export type ToolLabeller = (tc: ToolCall) => string;

/** One entry per harness. No fallthrough default -- see ``toolLabelFor``. */
const TOOL_LABEL_BY_HARNESS: Record<string, ToolLabeller> = {
  claude: claudeToolLabel,
  codex: codexToolLabel,
};

/**
 * The in-flight tool caption for ``harness``.
 *
 * An unregistered harness falls back to the claude labeller, which renders the
 * raw tool name rather than throwing -- an unknown harness should degrade the
 * caption, not break the activity strip.
 */
export function toolLabelFor(harness: string, tc: ToolCall): string {
  return (TOOL_LABEL_BY_HARNESS[harness] ?? claudeToolLabel)(tc);
}

export { claudeToolLabel } from "./claude";
export { codexToolLabel } from "./codex";
