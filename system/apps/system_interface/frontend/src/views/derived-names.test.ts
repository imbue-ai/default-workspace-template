import { describe, expect, it } from "vitest";

import { appServiceDisplayName, browserDisplayName, chatDisplayName, terminalDisplayName } from "./derived-names";

describe("chatDisplayName", () => {
  it("prefers the display_name label mngr holds for the agent", () => {
    expect(chatDisplayName({ name: "Chat-2", display_name: "Chat 2" })).toBe("Chat 2");
  });

  it("falls back to the true name for a pre-label agent", () => {
    // Chats created before the display_name label existed carry only their
    // true name; it is what every surface should keep calling them.
    expect(chatDisplayName({ name: "rich-stylish-sawfish" })).toBe("rich-stylish-sawfish");
    expect(chatDisplayName({ name: "Chat-1", display_name: null })).toBe("Chat-1");
    expect(chatDisplayName({ name: "Chat-1", display_name: "" })).toBe("Chat-1");
  });
});

describe("terminalDisplayName", () => {
  it("derives Terminal N from an allocator-minted session name", () => {
    expect(terminalDisplayName("terminal-1")).toBe("Terminal 1");
    expect(terminalDisplayName("terminal-12")).toBe("Terminal 12");
  });

  it("shows a hand-made or renamed session as itself", () => {
    expect(terminalDisplayName("dev")).toBe("dev");
    expect(terminalDisplayName("terminal-dev")).toBe("terminal-dev");
    expect(terminalDisplayName("terminal-")).toBe("terminal-");
  });
});

describe("browserDisplayName", () => {
  it("derives Browser N from a daemon-minted name", () => {
    expect(browserDisplayName("browser-1")).toBe("Browser 1");
    expect(browserDisplayName("browser-42")).toBe("Browser 42");
  });

  it("keeps a legacy random name visible, prefixed with its kind", () => {
    // Browsers created by older builds have random english names; the row
    // still has to say it is a browser.
    expect(browserDisplayName("alex-smith")).toBe("Browser alex-smith");
    expect(browserDisplayName("browser-abc")).toBe("Browser browser-abc");
  });
});

describe("appServiceDisplayName", () => {
  it("calls the built-in files service what its rail row does", () => {
    expect(appServiceDisplayName("files")).toBe("File Viewer");
  });

  it("shows every other app as its registered name", () => {
    expect(appServiceDisplayName("docs")).toBe("docs");
    expect(appServiceDisplayName("my-files")).toBe("my-files");
  });
});
