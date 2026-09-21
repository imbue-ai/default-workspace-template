// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

type ClientIdentityModule = typeof import("./ClientIdentity");

/** A fresh module: the client id is cached in module state on first read. */
async function loadClientIdentity(): Promise<ClientIdentityModule> {
  vi.resetModules();
  return await import("./ClientIdentity");
}

afterEach(() => {
  localStorage.clear();
});

describe("getClientId", () => {
  it("mints an id once, keeps it in storage, and reads the stored one back", async () => {
    const first = await loadClientIdentity();
    const minted = first.getClientId();
    expect(minted).not.toBe("");
    expect(first.getClientId()).toBe(minted);
    expect(localStorage.getItem("si-client-id")).toBe(minted);

    const second = await loadClientIdentity();
    expect(second.getClientId()).toBe(minted);
  });
});

describe("adoptClientIdentity", () => {
  it("takes the shell's client id and desktop without writing storage", async () => {
    const identity = await loadClientIdentity();
    expect(identity.getActiveDesktopId()).toBe("");

    identity.adoptClientIdentity({ clientId: "shell-client", desktopId: "research" });

    expect(identity.getClientId()).toBe("shell-client");
    expect(identity.getActiveDesktopId()).toBe("research");
    expect(localStorage.getItem("si-client-id")).toBeNull();
  });
});
