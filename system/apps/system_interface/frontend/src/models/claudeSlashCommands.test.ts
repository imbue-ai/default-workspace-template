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

  it("lists every command with a leading slash and no whitespace", () => {
    for (const command of INPUT_BLOCKING_SLASH_COMMANDS) {
      expect(command).toMatch(/^\/[a-z0-9-]+$/);
    }
  });
});
