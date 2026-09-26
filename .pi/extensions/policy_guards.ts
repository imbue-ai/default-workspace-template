// This workspace's tool-call guards, for pi.
//
// pi has no shell-hook surface, so the hook scripts claude and codex run from
// `.claude/settings.json` / `.codex/hooks.json` reach a pi agent through this
// extension instead: pi auto-discovers `.pi/extensions/*.ts` from the project and
// calls every extension's `tool_call` handler. Each guard is fed the same
// claude-shaped payload the hooks get on stdin, so the rule and its wording live
// once, in system/scripts/ -- the agy shim does the same thing from bash.
//
// The guards run first, on the command the agent wrote; the OOM self-tag and git
// identity are prepended only once they all pass, so no guard ever sees the prefix.
//
// See system/apps/chat/imbue/chat/harnesses/core-contracts/tool-call-policies.md for
// what each one enforces, and tool-call-policies-state-of-things.md beside it for how
// the other harnesses reach it.

import { spawnSync, type SpawnSyncReturns } from "node:child_process";
import { appendFileSync } from "node:fs";
import { join } from "node:path";

const WORK_DIR = process.env.MNGR_AGENT_WORK_DIR || process.cwd();
const SCRIPTS = join(WORK_DIR, "system", "scripts");
// A file, not stderr: pi is a TUI, and stderr would land on the agent's screen.
const LOG_PATH = join(process.env.MNGR_AGENT_STATE_DIR || "/tmp", "pi_policy_guards.log");

interface Guard {
  script: string;
  match: RegExp;
  // Whether the guard also polices pi's non-shell tools (read, edit, ...). Its `match`
  // then runs against the serialized payload rather than the command.
  allTools?: boolean;
}

// claude's PreToolUse order. `match` is a loose superset of what each script can
// refuse, so a call no guard has anything to say about spawns no guard process.
const GUARDS: Guard[] = [
  { script: join(SCRIPTS, "agent_prevent_commit_rewrite.sh"), match: /\bgit\b/ },
  { script: join(SCRIPTS, "agent_block_pipe_tail_head.sh"), match: /\b(tail|head)\b/ },
  {
    script: join(SCRIPTS, "agent_latchkey_request_standalone.sh"),
    match: /permission-requests|request_secret\.py/,
  },
  // A `read` of a secret file defeats P9 as surely as a `cat`, so this one runs on
  // every tool call.
  { script: join(SCRIPTS, "agent_secrets_guard.sh"), match: /\.secrets|\.mcpc/, allTools: true },
  // `ticket` does not contain `tk`, so a substring test would miss the spelling the
  // checker accepts.
  { script: join(SCRIPTS, "agent_tk_standalone.sh"), match: /\b(tk|ticket)\b/ },
];

const REWRITE_SCRIPT = join(SCRIPTS, "agent_rewrite_bash_command.py");

// BASH_ENV and sh's ENV are dropped: a startup file writing to stderr would be
// indistinguishable from a guard's own output, and would become the whole reason for
// a silent refusal.
function scriptEnv() {
  return { ...process.env, BASH_ENV: undefined, ENV: undefined };
}

/** Record a fail-open path, so a guard that stopped running is visible. Never throws. */
function note(message: string): void {
  try {
    appendFileSync(LOG_PATH, `${new Date().toISOString()} ${message}\n`);
  } catch {
    // Nowhere left to report to; the command must still run.
  }
}

function describeFailure(result: SpawnSyncReturns<string>): string {
  if (result.error) return String(result.error);
  const stderr = typeof result.stderr === "string" ? result.stderr.trim() : "";
  return `exited ${result.status ?? result.signal}${stderr ? `: ${stderr}` : ""}`;
}

/** The reason to refuse the call, from the first guard that refuses it, else null.
 *
 * `command` is the agent's shell command for a bash call and null for any other tool,
 * which only the `allTools` guards see. Only exit 2 refuses, as on claude. Anything
 * else -- a missing script (bash exits 127), a crash -- lets the call through and is
 * noted in LOG_PATH: a broken guard must not block every call. Never throws. */
function refusalReason(toolName: string, command: string | null, toolInput: unknown): string | null {
  const payload =
    command === null
      ? JSON.stringify({ tool_name: toolName, tool_input: toolInput ?? {} })
      : JSON.stringify({ tool_name: "Bash", tool_input: { command } });
  for (const guard of GUARDS) {
    if (command === null && !guard.allTools) continue;
    if (!guard.match.test(guard.allTools ? payload : (command ?? ""))) continue;
    try {
      const result = spawnSync("bash", [guard.script], { input: payload, encoding: "utf-8", env: scriptEnv() });
      if (result.status === 2) {
        const reason = typeof result.stderr === "string" ? result.stderr.trim() : "";
        return reason || "Blocked by a policy check.";
      }
      if (result.status !== 0) note(`guard ${guard.script} ${describeFailure(result)}; failing open`);
    } catch (error) {
      note(`guard ${guard.script} threw ${String(error)}; failing open`);
    }
  }
  return null;
}

/** The OOM self-tag and git-identity prefix, or "" if it cannot be built.
 *
 * `--prefix-only` keeps the command out of the round-trip, so what runs is exactly
 * what the agent wrote with the prefix in front. Never throws. */
function rewritePrefix(): string {
  try {
    const result = spawnSync("python3", [REWRITE_SCRIPT, "--prefix-only"], { encoding: "utf-8", env: scriptEnv() });
    if (result.status === 0 && typeof result.stdout === "string") return result.stdout;
    note(`rewrite ${REWRITE_SCRIPT} ${describeFailure(result)}; running the command unprefixed`);
  } catch (error) {
    note(`rewrite ${REWRITE_SCRIPT} threw ${String(error)}; running the command unprefixed`);
  }
  return "";
}

export default function policyGuards(pi: any): void {
  pi.on("tool_call", (event: any) => {
    const toolName = event?.toolName;
    if (typeof toolName !== "string") return;
    const input = event.input;
    if (toolName !== "bash") {
      const reason = refusalReason(toolName, null, input);
      if (reason !== null) return { block: true, reason };
      return;
    }
    const command = input?.command;
    if (typeof command !== "string" || !command) return;
    const reason = refusalReason(toolName, command, input);
    if (reason !== null) return { block: true, reason };
    input.command = rewritePrefix() + command;
  });
}
