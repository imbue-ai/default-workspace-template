import { type Plugin } from "@opencode-ai/plugin"

// tool.execute.after (opencode): soft-block-equivalent for substantive tool
// calls when the agent has no in_progress tk step record. Direct port of
// scripts/claude_require_steps_pretool.sh's logic, but genuine 1:1 parity
// with claude's mechanism is possible here (unlike antigravity) because
// tool.execute.after gives both the tool name AND a mutable output.output
// (the actual result text the model reads next) in one hook -- append the
// reminder directly onto the tool's own result instead of blocking it.
//
// Placed at project root .opencode/plugin/ (singular), matching the path
// mngr_opencode's own plugin uses under $OPENCODE_CONFIG_DIR/plugin/*.ts
// ("verified live" per that plugin's own source comment) -- opencode's docs
// site separately describes a plural .opencode/plugins/ project convention,
// which conflicts; both path segments appear in the installed opencode
// binary's strings. Went with the empirically-verified singular form; if
// this plugin doesn't load in practice, try the plural path as a fallback.
//
// Real opencode built-in tool names confirmed: read, edit, glob, grep, list,
// bash, task, external_directory, todowrite, question, webfetch, websearch,
// lsp, doom_loop, skill. Substantive (require a step): bash, edit, task,
// external_directory. Everything else is read-only/meta and skipped.
const SUBSTANTIVE_TOOLS = new Set(["bash", "edit", "task", "external_directory"])

const TK_INVOCATION_RE = /(^|\/|\s)(tk|ticket)\s/

export const RequireStepsPlugin: Plugin = async ({ $, directory }) => {
  return {
    "tool.execute.after": async (input, output) => {
      if (!SUBSTANTIVE_TOOLS.has(input.tool)) return

      if (input.tool === "bash") {
        const command = String((input.args as Record<string, unknown> | undefined)?.command ?? "")
        if (TK_INVOCATION_RE.test(command)) return
      }

      // `||`, not `??`: TICKETS_DIR can legitimately be exported as an empty
      // string (a real, common way env vars end up empty rather than unset),
      // and `??` only substitutes on null/undefined, not "" -- unlike the
      // ported bash hooks' `${TICKETS_DIR:-default}`, which does fall back
      // on empty too. An empty string is never a valid directory path, so
      // there's no legitimate falsy-but-meaningful value being lost here.
      const ticketsDir = process.env.TICKETS_DIR || `${directory}/.tickets`
      const tkScript = `${directory}/vendor/tk/ticket`

      const dirCheck = await $`test -d ${ticketsDir}`.quiet().nothrow()
      if (dirCheck.exitCode !== 0) return

      const execCheck = await $`test -x ${tkScript}`.quiet().nothrow()
      if (execCheck.exitCode !== 0) return

      const env = { ...process.env, TICKETS_DIR: ticketsDir }
      const inProgress = (
        await $`${tkScript} steps --status=in_progress`.env(env).quiet().nothrow()
      ).stdout
        .toString()
        .trim()
      if (inProgress) return

      const openSteps = (await $`${tkScript} steps`.env(env).quiet().nothrow()).stdout
        .toString()
        .trim()

      const reminder = openSteps
        ? "\n\n[Step tracking reminder]\n\nYou have declared step records but none is currently in_progress. Call `tk start <id>` on your next step before doing more work. Steps must be serial -- only one in_progress at a time."
        : '\n\n[Step tracking reminder]\n\nYou did substantive work without declaring any step records. The chat progress view requires steps to render your work as a structured timeline.\n\nBefore continuing, declare your plan as step records (each prints `Created <id>: <title>`):\n  tk create --step "Description of first step"\n  tk create --step "Description of second step"\n  ...\nThen start the first step with its literal id: tk start <id>\n\nSee AGENTS.md > Task management for the full protocol.'

      output.output += reminder
    },
  }
}
