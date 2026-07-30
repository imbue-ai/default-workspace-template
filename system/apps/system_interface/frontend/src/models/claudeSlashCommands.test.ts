import { describe, expect, it } from "vitest";
import { findDeclinedSlashCommand, listDeclinedSlashCommands } from "./claudeSlashCommands";

describe("findDeclinedSlashCommand", () => {
  it("declines a command that takes over the input box", () => {
    expect(findDeclinedSlashCommand("/status")).toEqual({ command: "/status", reason: "takes-over-input" });
  });

  it("declines a command that ends the session, with its own reason", () => {
    expect(findDeclinedSlashCommand("/exit")).toEqual({ command: "/exit", reason: "ends-session" });
    expect(findDeclinedSlashCommand("/quit")).toEqual({ command: "/quit", reason: "ends-session" });
  });

  it("ignores surrounding whitespace and case", () => {
    expect(findDeclinedSlashCommand("  /STATUS  ")?.command).toBe("/status");
    expect(findDeclinedSlashCommand("/Exit")?.command).toBe("/exit");
  });

  it("declines even with trailing arguments, which Claude ignores for these commands", () => {
    expect(findDeclinedSlashCommand("/status extra words")?.command).toBe("/status");
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
    expect(findDeclinedSlashCommand("/theme")?.command).toBe("/theme");
    expect(findDeclinedSlashCommand("/theme dark")?.command).toBe("/theme");
  });

  it("leaves commands that were measured to send fine", () => {
    // Verified against a live claude 2.1.220 agent: these keep the input box and send normally,
    // even though several of them render an interactive component.
    for (const command of ["/clear", "/compact", "/model", "/plugin", "/rewind", "/version", "/export"]) {
      expect(findDeclinedSlashCommand(command), command).toBeNull();
    }
  });

  it("lists every command with a leading slash and no whitespace", () => {
    for (const command of listDeclinedSlashCommands()) {
      expect(command).toMatch(/^\/[a-z0-9-]+$/);
    }
  });
});
