import { type Plugin } from "@opencode-ai/plugin"

// tool.execute.before (opencode): port of claude_prevent_commit_rewrite.sh
// and scripts/codex_tk_standalone.sh's checker call, combined into one
// hook -- opencode's tool.execute.before can only block via throwing, so
// both the inline rewrite-detection and the tk-standalone check (shelling
// out to the same unmodified claude_tk_standalone_check.py) live here
// together rather than as two hooks, unlike claude/codex/antigravity where
// they're separate PreToolUse hook scripts.
//
// Omitted: claude_tk_standalone.sh's MNGR_CLAUDE_SUBAGENT_PROXY_CHILD skip
// (subagents manage their own progress view) -- fct disables the
// claude_subagent_proxy plugin entirely (.mngr/settings.toml's
// disable_plugin__extend), so that env var is never set in this workspace
// and the check is already dead code today; omitted here rather than
// ported as inert code.
export const PreventCommitRewritePlugin: Plugin = async ({ $, directory }) => {
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash") return
      const command = String((output.args as Record<string, unknown> | undefined)?.command ?? "")
      if (!command) return

      // Matches "git <verb>" at the start of the command OR right after a
      // shell chain operator (&&, ;, |) -- a bare ^git anchor is trivially
      // bypassed by `git add -A && git commit --amend` (found by code
      // review). Same fix applied to the claude/codex/antigravity ports.
      const chainAnchor = "(^|&&|;|\\|)\\s*"
      if (new RegExp(chainAnchor + "git\\s+rebase").test(command)) {
        throw new Error("Blocked: git rebase commands are not allowed")
      }
      if (new RegExp(chainAnchor + "git\\s+pull").test(command)) {
        if (command.includes("--rebase") || /(^|\s)-r(\s|$)/.test(command)) {
          throw new Error("Blocked: git pull --rebase commands are not allowed (use git pull --merge instead)")
        }
      }
      if (new RegExp(chainAnchor + "git\\s+commit").test(command)) {
        if (command.includes("--amend") || command.includes("--fixup")) {
          throw new Error("Blocked: git commit with --amend or --fixup is not allowed")
        }
      }

      if (/(^|\/|\s)(tk|ticket)\s/.test(command)) {
        const check = await $`python3 ${directory}/scripts/claude_tk_standalone_check.py ${command}`
          .quiet()
          .nothrow()
        if (check.exitCode !== 0) {
          throw new Error(check.stderr.toString().trim())
        }
      }
    },
  }
}
