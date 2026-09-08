/**
 * The code-quality checks every package of the npm workspace runs as part of its vitest
 * suite: eslint over its sources, and prettier through the package's own `format:check`
 * script, so the globs each package declares stay the single source of truth.
 */
import { execSync } from "child_process";
import { describe, expect, it } from "vitest";

function run(command: string, cwd: string): { exitCode: number; output: string } {
  try {
    const output = execSync(command, { cwd, encoding: "utf-8", stdio: "pipe" });
    return { exitCode: 0, output };
  } catch (error) {
    const execError = error as { status: number; stdout: string; stderr: string };
    return { exitCode: execError.status, output: `${execError.stdout}\n${execError.stderr}` };
  }
}

// These checks shell out to eslint / prettier via execSync. Those processes
// (especially eslint's cold start) routinely take several seconds and can
// exceed Vitest's 5s default when the suite runs under load, producing a
// spurious timeout failure even though the lint/format check itself is clean.
// Give them generous headroom so the result reflects the tools, not the clock.
const TOOL_TIMEOUT_MS = 60_000;

/** Register the lint and format checks of the package rooted at `packageRoot`. */
export function describeLintAndFormat(packageRoot: string): void {
  describe("code quality", () => {
    it(
      "eslint produces no issues",
      () => {
        const result = run("npx eslint src/", packageRoot);
        expect(result.exitCode, `eslint found issues:\n${result.output}`).toBe(0);
      },
      TOOL_TIMEOUT_MS,
    );

    it(
      "prettier formatting has been applied",
      () => {
        const result = run("npm run --silent format:check", packageRoot);
        expect(result.exitCode, `prettier found unformatted files:\n${result.output}`).toBe(0);
      },
      TOOL_TIMEOUT_MS,
    );
  });
}
