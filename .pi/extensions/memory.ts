// The workspace's shared memory, for pi.
//
// Claude chats keep notes in data/memories/ through Claude Code's built-in memory. pi has no
// memory of its own, so on every prompt this asks system/scripts/agent_memory_context.py for
// the same thing Claude Code gives Claude -- how to keep a note, and the MEMORY.md index as it
// is now -- and puts it in the turn's system prompt. Reading it per prompt means a note another
// chat saved, or one the user edited or deleted in the "What agents know" app, reaches pi on
// its next message.
//
// The text goes in as its own system prompt section, the form pi records as a delta. If another
// extension already replaced the whole prompt for this turn (tk_workflow.ts does, to carry open
// steps), a section would no longer be rendered, so the text is appended to that prompt
// instead. pi loads project extensions in directory order, so either can run first.
//
// Fails open: if the script fails or prints nothing, the turn runs without memory.

import { spawnSync } from "node:child_process";
import { join } from "node:path";

const WORK_DIR = process.env.MNGR_AGENT_WORK_DIR || process.cwd();
const CONTEXT_SCRIPT = join(WORK_DIR, "system", "scripts", "agent_memory_context.py");
const HARNESS = "pi-coding";
const SECTION_NAME = "workspace_memory";
const SCRIPT_TIMEOUT_MS = 5000;

function note(message: string): void {
  process.stderr.write(`[workspace-memory] ${message}\n`);
}

/** The memory text for this turn, or null when it cannot be had. Never throws. */
function memoryContext(): string | null {
  try {
    const result = spawnSync("python3", [CONTEXT_SCRIPT, "--harness", HARNESS], {
      encoding: "utf-8",
      timeout: SCRIPT_TIMEOUT_MS,
    });
    if (result.status === 0 && typeof result.stdout === "string" && result.stdout.trim() !== "") {
      return result.stdout.trim();
    }
    if (result.status !== 0) {
      note(`${CONTEXT_SCRIPT} exited ${result.status ?? result.signal}; running this turn without memory`);
    }
  } catch (error) {
    note(`${CONTEXT_SCRIPT} threw ${String(error)}; running this turn without memory`);
  }
  return null;
}

export default function workspaceMemory(pi: any): void {
  pi.on("before_agent_start", (event: any) => {
    const context = memoryContext();
    if (context === null) return undefined;
    const options = event?.systemPromptOptions;
    if (options && typeof options === "object" && options.forceSystemPrompt === undefined) {
      options.sections = { ...(options.sections ?? {}), [SECTION_NAME]: context };
      return undefined;
    }
    const base = typeof event?.systemPrompt === "string" ? event.systemPrompt : "";
    return { systemPrompt: `${base}\n\n${context}` };
  });
}
