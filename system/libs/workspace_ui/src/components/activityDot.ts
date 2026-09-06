/* The pulsing "working" dot, shared so the pulse timing and colour cannot
 * drift between its call sites; size is per-site via `extra`. The
 * agent-activity-pulse keyframes live in style.css. */

export function activityDotClass(extra = ""): string {
  const parts = ["shrink-0 rounded-full bg-accent animate-[agent-activity-pulse_1.4s_ease-in-out_infinite]"];
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}
