import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildSessionTerminalUrl } from "./AgentManager";

/** Read back the repeated ``arg`` query params in order. */
function parseArgs(url: string): string[] {
  const query = url.split("?")[1] ?? "";
  return new URLSearchParams(query).getAll("arg");
}

describe("buildSessionTerminalUrl", () => {
  beforeEach(() => {
    // The terminal service origin is derived from the shell's own location
    // (see src/origin.ts); vitest's node environment has no ``window``, so
    // stand in a local workspace host.
    vi.stubGlobal("window", { location: { host: "agent-abc123.localhost:8421", protocol: "http:" } });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("emits the positional args in ttyd dispatch order on the terminal service's origin", () => {
    const url = buildSessionTerminalUrl("terminal-1", "term-abc", "/home/user/workspace");
    expect(url.startsWith("http://terminal.agent-abc123.localhost:8421/?")).toBe(true);
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-1", "term-abc", "/home/user/workspace"]);
  });

  it("omits the working directory arg as empty when none is given", () => {
    const url = buildSessionTerminalUrl("terminal-2", "term-xyz", "");
    expect(parseArgs(url)).toEqual(["_", "session", "terminal-2", "term-xyz", ""]);
  });

  it("percent-encodes special characters but round-trips the original values", () => {
    const url = buildSessionTerminalUrl("my term", "id", "/a b/c");
    // The raw query must not carry literal spaces...
    expect(url).not.toContain(" ");
    // ...but decoding recovers the exact session name and workdir.
    expect(parseArgs(url)).toEqual(["_", "session", "my term", "id", "/a b/c"]);
  });
});
