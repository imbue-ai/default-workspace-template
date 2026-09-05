import "../testing/dom";

import { describe, expect, it } from "vitest";

import {
  duplicateLiveKeyPanelIds,
  ensureLiveSurface,
  initializeLiveLayer,
  isPageAtListedUrl,
  liveKeyForPanel,
  liveSurfaceElement,
  liveSurfaceKeys,
  rekeyLiveSurface,
} from "./liveSurfaces";

describe("liveKeyForPanel", () => {
  it("files an instance panel under its address", () => {
    expect(liveKeyForPanel({ kind: "instance", address: "app:files", tabId: "tab-0000000000000001" })).toBe(
      "app:files",
    );
  });

  it("gives a launcher no key: it is a question about a pane, not an instance", () => {
    expect(liveKeyForPanel({ kind: "launcher" })).toBeNull();
    expect(liveKeyForPanel(undefined)).toBeNull();
  });
});

describe("duplicateLiveKeyPanelIds", () => {
  it("drops every occurrence of a key after the first, and never dedups launchers", () => {
    expect(
      duplicateLiveKeyPanelIds([
        { panelId: "a", key: "app:files" },
        { panelId: "b", key: null },
        { panelId: "c", key: "app:files" },
        { panelId: "d", key: null },
      ]),
    ).toEqual(["c"]);
  });
});

describe("isPageAtListedUrl", () => {
  it("is true only for the path the page itself reported, query and fragment included", () => {
    expect(isPageAtListedUrl("http://files.example/notes/?q=1", "/notes/?q=1")).toBe(true);
    expect(isPageAtListedUrl("http://files.example/notes/", "/notes/?q=1")).toBe(false);
    expect(isPageAtListedUrl("http://files.example/elsewhere/", "/notes/")).toBe(false);
    expect(isPageAtListedUrl("http://files.example/notes/", null)).toBe(false);
  });
});

describe("rekeyLiveSurface", () => {
  it("re-files the page under the new address and drops a page already filed there", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    initializeLiveLayer(host, () => {});
    const noMount = (): void => {};
    const moving = ensureLiveSurface(
      "app:terminal?instance=terminal-1",
      { kind: "instance", address: "app:terminal?instance=terminal-1", tabId: "tab-0000000000000001" },
      noMount,
    );
    const displaced = ensureLiveSurface(
      "app:terminal?instance=terminal-2",
      { kind: "instance", address: "app:terminal?instance=terminal-2", tabId: "tab-0000000000000002" },
      noMount,
    );

    rekeyLiveSurface("app:terminal?instance=terminal-1", "app:terminal?instance=terminal-2");

    expect(liveSurfaceKeys()).toEqual(["app:terminal?instance=terminal-2"]);
    expect(liveSurfaceElement("app:terminal?instance=terminal-2")).toBe(moving.element);
    expect(moving.key).toBe("app:terminal?instance=terminal-2");
    expect(displaced.element.isConnected).toBe(false);
    expect(Array.from(host.children)).toEqual([moving.element]);
  });
});
