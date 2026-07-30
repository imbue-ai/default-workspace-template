import { describe, expect, it } from "vitest";
import { DECLINED_SLASH_COMMANDS, findDeclinedSlashCommand } from "./claudeSlashCommands";

describe("findDeclinedSlashCommand", () => {
  it("declines a command that takes over the input box", () => {
    expect(findDeclinedSlashCommand("/status")).toBe("/status");
  });

  it("declines the session-ending commands too", () => {
    expect(findDeclinedSlashCommand("/exit")).toBe("/exit");
    expect(findDeclinedSlashCommand("/quit")).toBe("/quit");
  });

  it("ignores surrounding whitespace and case", () => {
    expect(findDeclinedSlashCommand("  /STATUS  ")).toBe("/status");
    expect(findDeclinedSlashCommand("/Exit")).toBe("/exit");
  });

  it("declines even with trailing arguments, which Claude ignores for these commands", () => {
    expect(findDeclinedSlashCommand("/status extra words")).toBe("/status");
  });

  it("does not match a command mentioned inside a sentence", () => {
    expect(findDeclinedSlashCommand("please run /status and tell me the model")).toBeNull();
  });

  it("does not match ordinary messages, near-misses, or empty input", () => {
    expect(findDeclinedSlashCommand("hello")).toBeNull();
    expect(findDeclinedSlashCommand("/statuses")).toBeNull();
    expect(findDeclinedSlashCommand("")).toBeNull();
    expect(findDeclinedSlashCommand("   ")).toBeNull();
  });

  it("declines the alias spellings, which a user can type interchangeably", () => {
    for (const alias of ["/cost", "/stats", "/settings", "/allowed-tools", "/bashes", "/quit"]) {
      expect(findDeclinedSlashCommand(alias), alias).not.toBeNull();
    }
  });

  it("declines /theme, whose argument form takes over even though the bare form does not", () => {
    expect(findDeclinedSlashCommand("/theme")).toBe("/theme");
    expect(findDeclinedSlashCommand("/theme dark")).toBe("/theme");
  });

  it("leaves commands that were measured to send fine", () => {
    // Verified against a live claude 2.1.220 agent: these keep the input box and send normally,
    // even though several of them render an interactive component.
    for (const command of ["/clear", "/compact", "/model", "/plugin", "/rewind", "/version", "/export"]) {
      expect(findDeclinedSlashCommand(command), command).toBeNull();
    }
  });

  it("lists every command with a leading slash and no whitespace", () => {
    for (const command of DECLINED_SLASH_COMMANDS) {
      expect(command).toMatch(/^\/[a-z0-9-]+$/);
    }
  });
});
