import { describe, expect, it } from "vitest";
import { findInputBlockingSlashCommand, INPUT_BLOCKING_SLASH_COMMANDS } from "./claudeSlashCommands";

describe("findInputBlockingSlashCommand", () => {
  it("matches a listed command", () => {
    expect(findInputBlockingSlashCommand("/status")).toBe("/status");
  });

  it("ignores surrounding whitespace and case", () => {
    expect(findInputBlockingSlashCommand("  /STATUS  ")).toBe("/status");
    expect(findInputBlockingSlashCommand("/Status")).toBe("/status");
  });

  it("matches even with trailing arguments, which Claude ignores for these commands", () => {
    expect(findInputBlockingSlashCommand("/status extra words")).toBe("/status");
  });

  it("does not match a command mentioned inside a sentence", () => {
    expect(findInputBlockingSlashCommand("please run /status and tell me the model")).toBeNull();
  });

  it("does not match ordinary messages, other commands, or empty input", () => {
    expect(findInputBlockingSlashCommand("hello")).toBeNull();
    expect(findInputBlockingSlashCommand("/clear")).toBeNull();
    expect(findInputBlockingSlashCommand("/statuses")).toBeNull();
    expect(findInputBlockingSlashCommand("")).toBeNull();
    expect(findInputBlockingSlashCommand("   ")).toBeNull();
  });

  it("matches the alias spellings, which a user can type interchangeably", () => {
    for (const alias of ["/cost", "/stats", "/settings", "/allowed-tools", "/bashes"]) {
      expect(findInputBlockingSlashCommand(alias), alias).toBe(alias);
    }
  });

  it("leaves commands that were measured to send fine", () => {
    // Verified against a live claude 2.1.220 agent: these keep the input box and send normally,
    // even though several of them render an interactive component.
    for (const command of ["/clear", "/compact", "/model", "/plugin", "/theme", "/rewind", "/version", "/export"]) {
      expect(findInputBlockingSlashCommand(command), command).toBeNull();
    }
  });

  it("lists every command with a leading slash and no whitespace", () => {
    for (const command of INPUT_BLOCKING_SLASH_COMMANDS) {
      expect(command).toMatch(/^\/[a-z0-9-]+$/);
    }
  });
});
