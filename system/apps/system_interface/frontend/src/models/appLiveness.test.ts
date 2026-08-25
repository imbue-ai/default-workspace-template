import { describe, expect, it } from "vitest";

import type { AppEntry } from "./AgentManager";
import { appStoppedDetail, isAppRunning, isAppStoppable, stoppedAppForServiceName } from "./appLiveness";

function app(overrides: Partial<AppEntry>): AppEntry {
  return { name: "web", url: "http://localhost:8000", label: "web-x7k9q2w1", ...overrides };
}

describe("isAppRunning", () => {
  it("treats an absent flag as running, so a server predating liveness dims nothing", () => {
    expect(isAppRunning(app({}))).toBe(true);
  });

  it("reads the flag when present", () => {
    expect(isAppRunning(app({ is_running: true }))).toBe(true);
    expect(isAppRunning(app({ is_running: false }))).toBe(false);
  });
});

describe("appStoppedDetail", () => {
  it("calls a supervised app stopped -- the workspace can start it again", () => {
    expect(appStoppedDetail(app({ program: "web", is_running: false }))).toBe("stopped");
  });

  it("says a program-less row is managed outside the workspace", () => {
    expect(appStoppedDetail(app({ is_running: false }))).toBe("not running (managed outside the workspace)");
  });
});

describe("isAppStoppable", () => {
  it("requires a supervised program", () => {
    expect(isAppStoppable(app({}))).toBe(false);
    expect(isAppStoppable(app({ program: "web" }))).toBe(true);
  });

  it("never offers to stop the essential services", () => {
    expect(isAppStoppable(app({ name: "system_interface", program: "system_interface" }))).toBe(false);
    expect(isAppStoppable(app({ name: "terminal", program: "terminal" }))).toBe(false);
  });

  it("offers to stop the browser fleet daemon -- it is stoppable at the service level", () => {
    expect(isAppStoppable(app({ name: "browser", program: "browser" }))).toBe(true);
  });
});

describe("stoppedAppForServiceName", () => {
  const apps = [app({ name: "docs", is_running: false, program: "docs" }), app({ name: "web" })];

  it("resolves a stopped app by service name", () => {
    expect(stoppedAppForServiceName(apps, "docs")?.name).toBe("docs");
  });

  it("answers null for a running app, an unknown name, and a null name", () => {
    expect(stoppedAppForServiceName(apps, "web")).toBeNull();
    expect(stoppedAppForServiceName(apps, "nope")).toBeNull();
    expect(stoppedAppForServiceName(apps, null)).toBeNull();
  });
});
