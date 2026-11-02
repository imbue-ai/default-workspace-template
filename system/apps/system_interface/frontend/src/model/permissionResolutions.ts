/**
 * The entries of the chrome's `minds:permission-resolutions` snapshot, validated: each is a
 * request id with the user's verdict. The keybindings delivery hands them to the shell's
 * backend, which matches them against the request it filed.
 */

export interface PermissionResolutionEntry {
  request_id: string;
  resolution: "granted" | "denied";
}

export function permissionResolutionEntries(message: Record<string, unknown>): PermissionResolutionEntry[] {
  const { resolutions } = message;
  if (!Array.isArray(resolutions)) return [];
  const entries: PermissionResolutionEntry[] = [];
  for (const entry of resolutions) {
    if (typeof entry !== "object" || entry === null) continue;
    const { requestId, resolution } = entry as Record<string, unknown>;
    if (typeof requestId !== "string" || requestId === "") continue;
    if (resolution !== "granted" && resolution !== "denied") continue;
    entries.push({ request_id: requestId, resolution });
  }
  return entries;
}
