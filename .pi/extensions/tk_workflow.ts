// This workspace's tk step discipline, for pi.
//
// The chat progress view is built from step records, so an agent has to declare and
// close them. claude and codex are held to that by the hook scripts in
// system/scripts/; pi has no shell-hook surface, so the rules are expressed here
// against the pi SDK instead. pi auto-discovers `.pi/extensions/*.ts` from the project
// and composes handlers across extensions -- `tool_result` handlers chain like
// middleware and `before_agent_start` chains the system prompt -- so this runs
// alongside mngr's lifecycle extension without either clobbering the other.
//
// Reminder text is copied verbatim from the scripts in system/scripts/ so all three
// harnesses read identically, and step state comes from the vendored `ticket` binary,
// the same source those scripts read.
//
//   * require-steps nudge   -> tool_result (pi's tool_call result cannot inject
//     non-blocking context, so the reminder rides the tool result -- same visible
//     effect, one tool-round later)
//   * open-steps carryover  -> before_agent_start (append to the turn's system prompt)
//   * open-steps stop nudge -> agent_settled (return values are ignored for that
//     event, so a stderr note is all it can be)

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const WORK_DIR = process.env.MNGR_AGENT_WORK_DIR || process.cwd();
const TICKET_SCRIPT = join(WORK_DIR, "system", "vendor", "tk", "ticket");
const TICKETS_DIR = process.env.TICKETS_DIR || join(WORK_DIR, ".tickets");

// pi's read-only tools -- the substantive-work reminder never fires for these
// (mirrors the skip list in agent_require_steps_pretool.sh).
const READONLY_TOOLS = new Set(["read", "grep", "find", "ls"]);

// A bash command that itself invokes tk/ticket -- the require-steps reminder skips
// it so the agent can freely create/manage steps (mirrors the script's tk skip).
const TK_COMMAND_RE = /(^|[|&;]\s*|\/)(tk|ticket)\s/;

// Reminder text copied verbatim from agent_require_steps_pretool.sh /
// agent_open_tickets_reminder.sh so every harness reads identically.
const REQUIRE_STEPS_NONE =
  "\n[Step tracking reminder]\n\n" +
  "You are about to do work without declaring any step records. The chat progress view requires steps to render your work as a structured timeline.\n\n" +
  "Before continuing, declare your plan as step records (each prints `Created <id>: <title>`):\n" +
  '  tk create --step "Description of first step"\n' +
  '  tk create --step "Description of second step"\n' +
  "  ...\n" +
  "Then start the first step with its literal id: tk start <id>\n\n" +
  "See CLAUDE.md > Task management for the full protocol.\n";
const REQUIRE_STEPS_NOT_STARTED =
  "\n[Step tracking reminder]\n\n" +
  "You have declared step records but none is currently in_progress. Call `tk start <id>` on your next step before doing more work. Steps must be serial -- only one in_progress at a time.\n";

/** Run `ticket steps [args]` and return non-empty step lines, or null when tk
 * cannot be consulted (no tickets dir / script). "" means consulted, no steps.
 * Never throws. */
function ticketSteps(args: string[]): string | null {
  if (!existsSync(TICKETS_DIR) || !existsSync(TICKET_SCRIPT)) return null;
  try {
    // Invoke via `bash` (the ticket script is bash) rather than exec'ing it directly,
    // so it works even when it sits on a noexec mount. TICKETS_DIR is exported explicitly:
    // the constant may be the WORK_DIR fallback (env var unset), and the child must read
    // the same tickets dir this guard checked -- the shell hooks these mirror export it too.
    const res = spawnSync("bash", [TICKET_SCRIPT, "steps", ...args], {
      encoding: "utf-8",
      env: { ...process.env, TICKETS_DIR },
    });
    // tk exits non-zero when there are no steps at all; that is "" (consulted), not null.
    const out =
      res.status === 0 && typeof res.stdout === "string" ? res.stdout : "";
    return out
      .split("\n")
      .filter((line) => line.trim() !== "")
      .join("\n");
  } catch {
    return null;
  }
}

/** The require-steps reminder to inject, or null to stay silent. Mirrors
 * agent_require_steps_pretool.sh: silent when a step is in_progress or tk can't be
 * consulted; "not started" when steps exist but none is in_progress; else "no steps". */
function requireStepsReminder(): string | null {
  const inProgress = ticketSteps(["--status=in_progress"]);
  if (inProgress === null || inProgress !== "") return null;
  const openAll = ticketSteps([]);
  if (openAll === null) return null;
  return openAll !== "" ? REQUIRE_STEPS_NOT_STARTED : REQUIRE_STEPS_NONE;
}

/** The open-steps carryover reminder to inject, or null when there are none.
 * Mirrors agent_open_tickets_reminder.sh. */
function carryoverReminder(): string | null {
  const openAll = ticketSteps([]);
  if (!openAll) return null;
  return (
    "\n[Open task reminder from default-workspace-template]\n\n" +
    "You have step records that are not yet closed:\n\n" +
    openAll +
    "\n\n" +
    "For each one, decide before continuing: keep working on it (call `tk start <id>` if it's not already in_progress), " +
    'replace it with a fresh step, or close it now with `tk close <id> "<summary>"` (the positional summary is required for steps). ' +
    "The summary is a concise one-line description of the *work done* in this step (the caption a non-technical user sees), not the outcome -- " +
    "the outcome goes in your final assistant message. Steps are sequential: do not start a new step until the previous one is closed.\n\n" +
    "See CLAUDE.md > Task management for the full protocol.\n"
  );
}

/** Count this agent's still-open step records (for the stop nudge). */
function openStepCount(): number {
  const openAll = ticketSteps([]);
  return openAll
    ? openAll.split("\n").filter((line) => line.trim() !== "").length
    : 0;
}

export default function tkWorkflow(pi: any): void {
  // Require-steps soft nudge: a substantive tool ran with no in-progress step, so the
  // reminder rides the result the model reads. Skipped for read-only tools and for bash
  // commands that invoke tk itself.
  pi.on("tool_result", (event: any) => {
    try {
      const toolName = event?.toolName;
      if (typeof toolName !== "string" || READONLY_TOOLS.has(toolName)) return undefined;
      if (toolName === "bash") {
        const command = event?.input?.command;
        if (typeof command === "string" && TK_COMMAND_RE.test(command)) return undefined;
      }
      const reminder = requireStepsReminder();
      if (reminder === null) return undefined;
      const content = Array.isArray(event?.content) ? event.content : [];
      return { content: [...content, { type: "text", text: reminder }] };
    } catch {
      return undefined;
    }
  });

  // Open-steps carryover: a new turn starting with still-open steps gets them appended
  // to its system prompt, the guaranteed model-visible channel.
  pi.on("before_agent_start", (event: any) => {
    try {
      const reminder = carryoverReminder();
      if (reminder === null) return undefined;
      const base = typeof event?.systemPrompt === "string" ? event.systemPrompt : "";
      return { systemPrompt: `${base}\n\n${reminder}` };
    } catch {
      return undefined;
    }
  });

  // Open-steps stop nudge.
  pi.on("agent_settled", () => {
    try {
      const count = openStepCount();
      if (count > 0) {
        process.stderr.write(
          `[task-management] Stopping with ${count} step record(s) still open. ` +
            "They'll appear at the top of the next turn's progress block.\n",
        );
      }
    } catch {
      // A nudge is not worth failing a settled run over.
    }
  });
}
