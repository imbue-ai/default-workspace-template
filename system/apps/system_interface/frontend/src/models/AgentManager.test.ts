import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildSessionTerminalUrl, handleEvent, whenAppRegistered } from "./AgentManager";
import type { AppEntry } from "./AgentManager";

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
    vi.stubGlobal("window", {
      location: { host: "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421", protocol: "http:" },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("emits the positional args in ttyd dispatch order on the terminal service's origin", () => {
    const url = buildSessionTerminalUrl("terminal-1", "term-abc", "/home/user/workspace");
    expect(url.startsWith("http://terminal.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/?")).toBe(true);
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

describe("whenAppRegistered", () => {
  /** Feed the module an ``apps_updated`` frame naming exactly ``names``. The
   *  frame is a full replace, which is also how the module's state is put into
   *  a known shape at the start of each case (it is module-level, so it carries
   *  across cases in this file). */
  function registerApps(...names: string[]): void {
    const apps: AppEntry[] = names.map((name) => ({ name, url: `http://${name}.test/`, label: `${name}-x7k9` }));
    handleEvent({ type: "apps_updated", apps });
  }

  beforeEach(() => {
    vi.useFakeTimers();
    registerApps();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("resolves at once for an app that is already registered", async () => {
    registerApps("web");
    await expect(whenAppRegistered("web")).resolves.toBe(true);
  });

  it("resolves when the app arrives in a later frame", async () => {
    const pending = whenAppRegistered("web");
    registerApps("web");
    await expect(pending).resolves.toBe(true);
  });

  it("keeps waiting through a frame that carries only other apps", async () => {
    // The reason this exists rather than a ``whenAppsLoaded`` + membership
    // check: the shell registers ITSELF, so the app list goes non-empty at boot
    // no matter which other services are up. A waiter on a slower app must not
    // be woken by that frame.
    const pending = whenAppRegistered("web");
    let isSettled = false;
    void pending.then(() => (isSettled = true));

    registerApps("system_interface");
    await Promise.resolve();
    expect(isSettled).toBe(false);

    registerApps("system_interface", "web");
    await expect(pending).resolves.toBe(true);
  });

  it("gives up with false once the budget runs out", async () => {
    const pending = whenAppRegistered("web", 5000);
    vi.advanceTimersByTime(5000);
    await expect(pending).resolves.toBe(false);
  });

  it("stays on its verdict when the app shows up after giving up", async () => {
    const pending = whenAppRegistered("web", 5000);
    vi.advanceTimersByTime(5000);
    await expect(pending).resolves.toBe(false);

    // Late arrival: the waiter is already settled and gone, so this must not
    // flip the verdict or throw on a stale entry.
    registerApps("web");
    await expect(pending).resolves.toBe(false);
  });
});
