// This workspace's tool-call guards, for pi.
//
// claude and codex reach the same checkers through hook wrappers, which read a hook
// payload on stdin; pi has no shell-hook surface, so it gets them here instead. pi
// auto-discovers `.pi/extensions/*.ts` from the project, and every extension's
// `tool_call` handler runs -- so this loads alongside mngr's own lifecycle extension
// and either may block.
//
// Two shapes of checker. A `command` checker takes the agent's shell command as $1 and
// runs on bash calls only. A `payload` checker reads a claude-shaped hook payload
// (`{tool_name, tool_input}`) on stdin and runs on every tool call, because the rule
// it enforces (a secret file is never opened directly) applies to pi's file tools as
// much as to its shell. Either exits 2 to refuse, with the reason on stderr. `match`
// keeps a checker off the calls it has nothing to say about, which matters because
// every entry here costs a process per tool call.
//
// See system/apps/chat/imbue/chat/harnesses/core-contracts/tool-call-policies.md for what each one enforces and how the harnesses
// harnesses reach it.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const WORK_DIR = process.env.MNGR_AGENT_WORK_DIR || process.cwd();
const SCRIPTS = join(WORK_DIR, "system", "scripts");

interface Checker {
  script: string;
  match: RegExp;
  input: "command" | "payload";
}

// `match` mirrors the prefilter each `.sh` wrapper uses, so pi refuses exactly what
// claude and codex refuse: the host path or the request script for a filing, the
// `tk`/`ticket` word for a step transition (`ticket` does not contain `tk`, so a
// substring test would miss the spelling the checker and the shared parser both
// accept), and the secrets directory for a direct read of a secret file. A payload
// checker's `match` runs against the serialized payload, a command checker's against
// the command.
const CHECKERS: Checker[] = [
  {
    script: join(SCRIPTS, "agent_latchkey_request_check.py"),
    match: /permission-requests|request_secret\.py/,
    input: "command",
  },
  { script: join(SCRIPTS, "agent_tk_standalone_check.py"), match: /\b(tk|ticket)\b/, input: "command" },
  { script: join(SCRIPTS, "agent_secrets_guard_check.py"), match: /\.secrets/, input: "payload" },
];

/** Run one checker and return its refusal reason, or null when it allows the call.
 *
 * The checker runs through bash so its own line keeps shell expansions, with the
 * agent's command passed as `$1` rather than interpolated -- a command containing
 * quotes or `$(...)` cannot rewrite the checker's line. A payload checker gets the
 * payload on stdin instead, for the same reason. BASH_ENV and sh's ENV are dropped:
 * a startup file writing to stderr would be indistinguishable from the checker's own
 * output, and would become the whole reason for a silent refusal. */
function runChecker(checker: Checker, command: string | null, payload: string): string | null {
  const argv =
    checker.input === "command"
      ? ["--noprofile", "--norc", "-c", `python3 "${checker.script}" "$1"`, "bash", command ?? ""]
      : ["--noprofile", "--norc", "-c", `python3 "${checker.script}"`];
  const result = spawnSync("bash", argv, {
    encoding: "utf-8",
    env: { ...process.env, BASH_ENV: undefined, ENV: undefined },
    input: checker.input === "payload" ? payload : undefined,
  });
  if (result.status === 2) {
    const reason = typeof result.stderr === "string" ? result.stderr.trim() : "";
    return reason || "Blocked by a policy check.";
  }
  return null;
}

/** The reason to refuse the call, from the first checker that refuses it, else null.
 *
 * `command` is the agent's shell command for a bash call and null otherwise; a command
 * checker only ever sees a bash call. Never throws. A broken checker must not block
 * every command. */
function refusalReason(toolName: string, command: string | null, toolInput: unknown): string | null {
  const payload = JSON.stringify({ tool_name: toolName, tool_input: toolInput ?? {} });
  for (const checker of CHECKERS) {
    if (checker.input === "command") {
      if (command === null || !checker.match.test(command)) continue;
    } else if (!checker.match.test(payload)) {
      continue;
    }
    // A checker that is not on disk refuses nothing: python3 exits 2 on a file it
    // cannot open, the same status a real refusal uses, so without this every
    // matching command would be blocked with a python error as its reason.
    if (!existsSync(checker.script)) continue;
    try {
      const reason = runChecker(checker, command, payload);
      if (reason !== null) return reason;
    } catch {
      // Fail open, per checker.
    }
  }
  return null;
}

/** The command the agent wrote, from a bash `tool_call` event, or null.
 *
 * `input.command` is mutable and mngr's lifecycle extension rewrites it in place,
 * prepending the OOM self-tag and git identity as their own `;`-joined commands. pi
 * runs every extension's `tool_call` handler on the same event without specifying
 * their order, so that rewrite may already have happened by the time we read it --
 * and the checkers would refuse the prefix as a command chained ahead of the
 * agent's, blocking every request and every step transition. mngr therefore records
 * the pre-rewrite command as `mngrOriginalCommand`; it is absent when we run first,
 * which is exactly when `input.command` is still untouched. */
function agentCommand(event: any): string | null {
  for (const candidate of [event?.mngrOriginalCommand, event?.input?.command]) {
    if (typeof candidate === "string" && candidate) return candidate;
  }
  return null;
}

export default function policyGuards(pi: any): void {
  pi.on("tool_call", (event: any) => {
    const toolName = event?.toolName;
    if (typeof toolName !== "string") return;
    const command = toolName === "bash" ? agentCommand(event) : null;
    if (toolName === "bash" && command === null) return;
    // The payload checker judges a bash call by the command the agent wrote, not the
    // rewritten one, for the same reason `agentCommand` exists.
    const toolInput = toolName === "bash" ? { ...(event?.input ?? {}), command } : event?.input;
    const reason = refusalReason(toolName, command, toolInput);
    if (reason !== null) return { block: true, reason };
  });
}
