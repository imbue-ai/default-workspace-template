// The workspace's shared memory, for pi.
//
// Claude chats keep notes in data/memories/ through Claude Code's built-in memory. pi has no
// memory of its own, so this gives pi the same two things Claude Code gives Claude.
//
//   * before_agent_start: on every prompt, ask system/scripts/agent_memory_context.py for how
//     to keep a note (the protocol) and the MEMORY.md index as it is now, with any notice of
//     notes the user deleted or edited. A note another chat saved, or one the user changed in the
//     "Agent Memory" app, reaches pi on its next message.
//   * tool_result: right after pi writes or edits a note, stamp its metadata.modified (and its
//     metadata.source when it names none), as Claude Code does for Claude's notes, so the date is
//     never the model's guess.
//
// The protocol and the index go in as two system prompt sections. pi records the prompt once and
// appends a section's full text again only when it changes, so the fixed protocol is recorded
// once per chat and the index only on messages where a note changed. Neither may depend on the
// clock, or every message would re-append it. If another extension already replaced the whole
// prompt for this turn (tk_workflow.ts does, to carry open steps), sections would no longer be
// rendered, so the text is appended to that prompt instead. pi loads project extensions in
// directory order, so either can run first.
//
// Fails open: if the script fails or prints nothing, the turn runs without memory, and a note
// that cannot be stamped stays as pi wrote it.

import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";

const WORK_DIR = process.env.MNGR_AGENT_WORK_DIR || process.cwd();
const CONTEXT_SCRIPT = join(WORK_DIR, "system", "scripts", "agent_memory_context.py");
// The script's default notes folder: the absolute one Claude's autoMemoryDirectory names.
const NOTES_DIR = join(homedir(), "workspace", "data", "memories");
const HARNESS = "pi-coding";
const PROTOCOL_SECTION = "workspace_memory_protocol";
const MEMORY_SECTION = "workspace_memory";
const NOTE_WRITING_TOOLS = new Set(["write", "edit"]);
const SCRIPT_TIMEOUT_MS = 5000;

interface MemorySections {
  readonly protocol: string;
  readonly memory: string;
}

function note(message: string): void {
  process.stderr.write(`[workspace-memory] ${message}\n`);
}

/** The protocol and the index for this turn, or null when they cannot be had. Never throws. */
function memorySections(): MemorySections | null {
  try {
    const result = spawnSync("python3", [CONTEXT_SCRIPT, "--harness", HARNESS, "--json"], {
      encoding: "utf-8",
      timeout: SCRIPT_TIMEOUT_MS,
    });
    if (result.status !== 0) {
      note(`${CONTEXT_SCRIPT} exited ${result.status ?? result.signal}; running this turn without memory`);
      return null;
    }
    if (typeof result.stdout !== "string" || result.stdout.trim() === "") return null;
    const parsed = JSON.parse(result.stdout);
    if (typeof parsed?.protocol !== "string" || typeof parsed?.memory !== "string") {
      note(`${CONTEXT_SCRIPT} printed an unexpected shape; running this turn without memory`);
      return null;
    }
    return { protocol: parsed.protocol.trim(), memory: parsed.memory.trim() };
  } catch (error) {
    note(`${CONTEXT_SCRIPT} failed: ${String(error)}; running this turn without memory`);
    return null;
  }
}

/** The note file a write or edit touched, or null when the call was not a successful one on a note. */
function writtenNote(event: any): string | null {
  if (event?.isError === true || !NOTE_WRITING_TOOLS.has(event?.toolName)) return null;
  const raw = event?.input?.path;
  if (typeof raw !== "string" || !raw.endsWith(".md")) return null;
  const path = resolve(process.cwd(), raw.startsWith("~/") ? join(homedir(), raw.slice(2)) : raw);
  return dirname(path) === NOTES_DIR ? path : null;
}

export default function workspaceMemory(pi: any): void {
  pi.on("before_agent_start", (event: any) => {
    const sections = memorySections();
    if (sections === null) return undefined;
    const options = event?.systemPromptOptions;
    if (options && typeof options === "object" && options.forceSystemPrompt === undefined) {
      options.sections = {
        ...(options.sections ?? {}),
        [PROTOCOL_SECTION]: sections.protocol,
        [MEMORY_SECTION]: sections.memory,
      };
      return undefined;
    }
    const base = typeof event?.systemPrompt === "string" ? event.systemPrompt : "";
    return { systemPrompt: `${base}\n\n${sections.protocol}\n\n${sections.memory}` };
  });

  pi.on("tool_result", (event: any) => {
    const path = writtenNote(event);
    if (path === null) return undefined;
    try {
      const result = spawnSync("python3", [CONTEXT_SCRIPT, "--stamp", path, "--harness", HARNESS], {
        encoding: "utf-8",
        timeout: SCRIPT_TIMEOUT_MS,
      });
      if (result.status !== 0) note(`stamping ${path} exited ${result.status ?? result.signal}; left as written`);
    } catch (error) {
      note(`stamping ${path} failed: ${String(error)}; left as written`);
    }
    return undefined;
  });
}
