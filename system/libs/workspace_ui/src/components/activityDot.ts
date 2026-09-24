/* The pulsing "working" dot, shared so the pulse timing and colour cannot
 * drift between its call sites; size is per-site via `extra`. The
 * agent-activity-pulse keyframes live in base.css. */

/** The dot's colour: the accent for work in progress, warning for waiting on something to come up. */
export type ActivityDotTone = "accent" | "warning";

const TONE_CLASS: Record<ActivityDotTone, string> = {
  accent: "bg-accent",
  warning: "bg-warning",
};

export function activityDotClass(extra = "", tone: ActivityDotTone = "accent"): string {
  const parts = [`shrink-0 rounded-full ${TONE_CLASS[tone]} animate-[agent-activity-pulse_1.4s_ease-in-out_infinite]`];
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}
