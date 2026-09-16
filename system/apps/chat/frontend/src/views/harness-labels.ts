/** The user-facing names of the harnesses (`HarnessType` on the backend). */

// An unknown harness shows its raw name rather than nothing.
const HARNESS_LABEL_BY_NAME: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  "pi-coding": "Pi",
  opencode: "OpenCode",
  antigravity: "Antigravity",
};

export function harnessLabel(harness: string): string {
  return HARNESS_LABEL_BY_NAME[harness] ?? harness;
}
