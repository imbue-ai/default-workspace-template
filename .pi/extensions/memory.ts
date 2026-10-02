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
//     never the model's guess. Then, and after any shell command that names the notes folder, set
//     each note's MEMORY.md line to the note's description, so the line every chat starts from
//     never contradicts the note.
//
// The protocol and the index go in as two system prompt sections. pi records the prompt once and
// appends a section's full text again only when it changes, so the fixed protocol is recorded
// once per chat and the index only on messages where a note changed. Neither may depend on the
// clock, or every message would re-append it.
//
// tk_workflow.ts replaces the whole prompt on turns with open steps. pi loads project extensions
// in raw directory order, so either may run first, and both orders keep the memory: run after
// this, it builds its prompt from event.systemPrompt, which pi renders from the sections set
// here; run before it, it has set forceSystemPrompt, and the text is appended to that prompt.
//
// Fails open: if the script fails or prints nothing, the turn runs without memory, and a note
// that cannot be stamped stays as pi wrote it. Both are logged to a file, not stderr, which
// lands on pi's screen.

import { spawnSync } from "node:child_process";
import { appendFileSync } from "node:fs";
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
// How a shell command names the folder, whether as data/memories or ~/workspace/data/memories.
const NOTES_DIR_IN_COMMANDS = "data/memories";
const SCRIPT_TIMEOUT_MS = 5000;
const LOG_PATH = join(process.env.MNGR_AGENT_STATE_DIR || "/tmp", "pi_workspace_memory.log");

interface MemorySections {
  readonly protocol: string;
  readonly memory: string;
}

function note(message: string): void {
  try {
    appendFileSync(LOG_PATH, `${new Date().toISOString()} ${message}\n`);
  } catch {
    // Logging must never cost the turn.
  }
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
    if (typeof result.stdout !== "string" || result.stdout.trim() === "") {
      const reason = typeof result.stderr === "string" ? result.stderr.trim() : "";
      note(`${CONTEXT_SCRIPT} printed nothing${reason ? `: ${reason}` : ""}; running this turn without memory`);
      return null;
    }
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

/** Whether a successful shell command named the notes folder, so it may have changed a note or the index. */
function touchesNotesByShell(event: any): boolean {
  if (event?.isError === true || event?.toolName !== "bash") return false;
  const command = event?.input?.command;
  return typeof command === "string" && command.includes(NOTES_DIR_IN_COMMANDS);
}

/** Runs the context script for a side effect on the notes; a failure is logged and the note left as it is. */
function runScript(args: readonly string[], what: string): void {
  try {
    const result = spawnSync("python3", [CONTEXT_SCRIPT, ...args], { encoding: "utf-8", timeout: SCRIPT_TIMEOUT_MS });
    if (result.status !== 0) note(`${what} exited ${result.status ?? result.signal}; left as written`);
  } catch (error) {
    note(`${what} failed: ${String(error)}; left as written`);
  }
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
    if (touchesNotesByShell(event)) {
      runScript(["--sync-index"], "syncing the memory index");
      return undefined;
    }
    const path = writtenNote(event);
    if (path !== null) runScript(["--stamp", path, "--harness", HARNESS], `stamping ${path}`);
    return undefined;
  });
}
