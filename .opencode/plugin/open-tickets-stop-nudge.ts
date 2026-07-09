import { type Plugin } from "@opencode-ai/plugin"

// event/session.idle (opencode): port of claude_open_tickets_stop_nudge.sh.
// Purely observational and non-blocking, same as the original -- just logs
// via console.error (opencode's own log capture) when the session goes
// idle with tk steps still open. No output/decision channel needed since
// this never blocks anything, matching the original's always-exit-0 design.
export const OpenTicketsStopNudgePlugin: Plugin = async ({ $, directory }) => {
  const parentBySession = new Map<string, string | undefined>()

  const isRootSession = (sessionID: string): boolean => {
    // Map.get() returns undefined both for "never observed" and for
    // "observed, and its parentID is genuinely undefined (a real root
    // session)" -- .has() first disambiguates the two (found by code
    // review). An unobserved session (e.g. the plugin reloaded mid-session,
    // so no session.created/updated ever repopulated the map) is treated
    // as NOT root: a missed nudge is a smaller failure than misattributing
    // a sub-agent's nudge to a delegated session that should be filtered out.
    if (!parentBySession.has(sessionID)) return false
    const parentID = parentBySession.get(sessionID)
    return parentID === undefined || parentID === ""
  }

  return {
    event: async ({ event }) => {
      if (event.type === "session.created" || event.type === "session.updated") {
        const info = (event.properties as { info?: { id: string; parentID?: string } }).info
        if (info) parentBySession.set(info.id, info.parentID)
        return
      }

      if (event.type !== "session.idle") return
      const sessionID = (event.properties as { sessionID: string }).sessionID
      if (!isRootSession(sessionID)) return

      // `||`, not `??` -- see require-steps.ts's identical fix for why.
      const ticketsDir = process.env.TICKETS_DIR || `${directory}/.tickets`
      const tkScript = `${directory}/vendor/tk/ticket`

      const dirCheck = await $`test -d ${ticketsDir}`.quiet().nothrow()
      if (dirCheck.exitCode !== 0) return
      const execCheck = await $`test -x ${tkScript}`.quiet().nothrow()
      if (execCheck.exitCode !== 0) return

      const env = { ...process.env, TICKETS_DIR: ticketsDir }
      const steps = (await $`${tkScript} steps`.env(env).quiet().nothrow()).stdout
        .toString()
        .split("\n")
        .filter((line) => line.trim().length > 0)

      if (steps.length > 0) {
        console.error(
          `[task-management] Session went idle with ${steps.length} step record(s) still open. They'll appear at the top of the next turn's progress block.`,
        )
      }
    },
  }
}
