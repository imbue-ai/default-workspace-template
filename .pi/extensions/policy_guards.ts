// This workspace's tool-call guards, for pi.
//
// claude and codex reach the same checkers through hook wrappers, which read a hook
// payload on stdin; pi has no shell-hook surface, so it gets them here instead. pi
// auto-discovers `.pi/extensions/*.ts` from the project, and every extension's
// `tool_call` handler runs -- so this loads alongside mngr's own lifecycle extension
// and either may block.
//
// Each checker takes the agent's command as $1 and exits 2 to refuse it, with the
// reason on stderr. `match` keeps a checker off the commands it has nothing to say
// about, which matters because every entry here costs a process per bash tool call.
//
// See system/scripts/POLICY_HOOKS.md for what each one enforces and how the three
// harnesses reach it.

import { spawnSync } from "node:child_process";
import { join } from "node:path";

const WORK_DIR = process.env.MNGR_AGENT_WORK_DIR || process.cwd();
const SCRIPTS = join(WORK_DIR, "system", "scripts");

interface Checker {
  command: string;
  match: string;
}

const CHECKERS: Checker[] = [
  { command: `python3 "${join(SCRIPTS, "agent_latchkey_request_check.py")}"`, match: "permission-requests" },
  { command: `python3 "${join(SCRIPTS, "agent_tk_standalone_check.py")}"`, match: "tk" },
];

/** The reason to refuse `command`, from the first checker that refuses it, else null.
 *
 * The checker runs through bash so its own line keeps shell expansions, with the
 * agent's command passed as `$1` rather than interpolated -- a command containing
 * quotes or `$(...)` cannot rewrite the checker's line. BASH_ENV and sh's ENV are
 * dropped: a startup file writing to stderr would be indistinguishable from the
 * checker's own output, and would become the whole reason for a silent refusal.
 *
 * Never throws. A broken checker must not block every command. */
function refusalReason(command: string): string | null {
  for (const checker of CHECKERS) {
    if (!command.includes(checker.match)) continue;
    try {
      const result = spawnSync("bash", ["--noprofile", "--norc", "-c", `${checker.command} "$1"`, "bash", command], {
        encoding: "utf-8",
        env: { ...process.env, BASH_ENV: undefined, ENV: undefined },
      });
      if (result.status === 2) {
        const reason = typeof result.stderr === "string" ? result.stderr.trim() : "";
        return reason || "Blocked by a policy check.";
      }
    } catch {
      // Fail open, per checker.
    }
  }
  return null;
}

export default function policyGuards(pi: any): void {
  pi.on("tool_call", (event: any) => {
    if (event?.toolName !== "bash") return;
    const command = event.input?.command;
    if (typeof command !== "string" || !command) return;
    const reason = refusalReason(command);
    if (reason !== null) return { block: true, reason };
  });
}
