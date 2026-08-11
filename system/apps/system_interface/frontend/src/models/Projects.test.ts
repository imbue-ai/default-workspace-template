import { afterEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  autosaveProject,
  chooseInitialProject,
  createProject,
  deleteProjectRequest,
  EVERYTHING_PROJECT_ID,
  fetchProjectContent,
  fetchProjectsList,
  updateProjectSettings,
  type ProjectInfo,
} from "./Projects";

const EVERYTHING: ProjectInfo = {
  project_id: EVERYTHING_PROJECT_ID,
  name: "Everything",
  color: "#8b8b8b",
  glyph: 0,
  has_content: true,
};
const WEBSITE: ProjectInfo = {
  project_id: "website-redesign",
  name: "Website Redesign",
  color: "#4f8ef7",
  glyph: 3,
  has_content: true,
};
const TAXES: ProjectInfo = { project_id: "taxes", name: "Taxes", color: "#e5a33d", glyph: 7, has_content: false };

/** Stand in for the backend with one canned response for every call. */
function stubFetch(response: Partial<Response>): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn(() => Promise.resolve(response as Response));
  vi.stubGlobal("fetch", mockFetch);
  return mockFetch;
}

/** Stand in for a server that cannot be reached at all. */
function stubUnreachableFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("offline"))),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("chooseInitialProject", () => {
  it("prefers the browser's stored choice when it still exists", () => {
    expect(chooseInitialProject([EVERYTHING, WEBSITE, TAXES], "taxes")).toBe(TAXES);
  });

  it("falls back to Everything when the stored choice is gone", () => {
    expect(chooseInitialProject([EVERYTHING, WEBSITE], "deleted-project")).toBe(EVERYTHING);
  });

  it("picks Everything on a first-ever connect", () => {
    expect(chooseInitialProject([WEBSITE, EVERYTHING], "")).toBe(EVERYTHING);
  });

  it("falls back to the first project when Everything is missing", () => {
    expect(chooseInitialProject([WEBSITE, TAXES], "")).toBe(WEBSITE);
    expect(chooseInitialProject([WEBSITE, TAXES], "deleted-project")).toBe(WEBSITE);
  });

  it("returns null when no projects exist", () => {
    expect(chooseInitialProject([], "anything")).toBeNull();
  });
});

describe("fetchProjectsList", () => {
  it("returns the registry the server sent", async () => {
    const mockFetch = stubFetch({
      ok: true,
      json: () => Promise.resolve({ projects: [EVERYTHING, WEBSITE], last_active_id: "website-redesign" }),
    });

    expect(await fetchProjectsList()).toEqual({
      projects: [EVERYTHING, WEBSITE],
      last_active_id: "website-redesign",
    });
    expect(mockFetch).toHaveBeenCalledWith("/api/projects");
  });

  it("yields an empty registry when the server rejects the read", async () => {
    stubFetch({ ok: false, status: 500, json: () => Promise.resolve({}) });

    // The workspace still has to render; it just will not persist anything.
    expect(await fetchProjectsList()).toEqual({ projects: [], last_active_id: null });
  });

  it("yields an empty registry when the server is unreachable", async () => {
    stubUnreachableFetch();

    expect(await fetchProjectsList()).toEqual({ projects: [], last_active_id: null });
  });

  it("tolerates a response missing either field", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({}) });

    expect(await fetchProjectsList()).toEqual({ projects: [], last_active_id: null });
  });
});

describe("fetchProjectContent", () => {
  it("returns the saved content and percent-encodes the id", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ layout: { dockview: { grid: {} } } }) });

    expect(await fetchProjectContent("my project")).toEqual({ dockview: { grid: {} } });
    expect(mockFetch).toHaveBeenCalledWith("/api/projects/my%20project");
  });

  it("returns null for a project that has never been saved", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({ layout: null }) });

    expect(await fetchProjectContent("taxes")).toBeNull();
  });

  it("returns null when the read fails, rather than throwing at startup", async () => {
    stubFetch({ ok: false, status: 404, json: () => Promise.resolve({}) });
    expect(await fetchProjectContent("gone")).toBeNull();

    stubUnreachableFetch();
    expect(await fetchProjectContent("taxes")).toBeNull();
  });
});

describe("autosaveProject", () => {
  it("posts the content under the active project and client id", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({}) });

    await autosaveProject("website-redesign", { dockview: { grid: {} } }, "client-7");

    expect(mockFetch).toHaveBeenCalledWith("/api/projects/website-redesign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ layout: { dockview: { grid: {} } }, client_id: "client-7" }),
    });
  });

  it("throws with the server's detail so the caller can report the lost save", async () => {
    stubFetch({ ok: false, status: 409, json: () => Promise.resolve({ detail: "unknown project" }) });

    await expect(autosaveProject("gone", {}, "client-7")).rejects.toThrow("unknown project");
  });

  it("throws with the status when the failure carries no detail", async () => {
    stubFetch({ ok: false, status: 500, json: () => Promise.reject(new Error("not json")) });

    await expect(autosaveProject("website-redesign", {}, "client-7")).rejects.toThrow("HTTP 500");
  });
});

describe("createProject", () => {
  it("posts the display metadata and returns the created project", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve(TAXES) });

    expect(await createProject("Taxes", "#e5a33d", 7)).toEqual(TAXES);
    expect(mockFetch).toHaveBeenCalledWith("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Taxes", color: "#e5a33d", glyph: 7 }),
    });
  });

  it("throws the server's rejection reason", async () => {
    stubFetch({ ok: false, status: 400, json: () => Promise.resolve({ detail: "project name already in use" }) });

    await expect(createProject("Taxes", "#e5a33d", 7)).rejects.toThrow("project name already in use");
  });
});

describe("updateProjectSettings", () => {
  it("posts to the project's settings endpoint and returns the updated project", async () => {
    const renamed = { ...WEBSITE, name: "Website", color: "#2f6fd0", glyph: 5 };
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve(renamed) });

    expect(await updateProjectSettings("website-redesign", "Website", "#2f6fd0", 5)).toEqual(renamed);
    expect(mockFetch).toHaveBeenCalledWith("/api/projects/website-redesign/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Website", color: "#2f6fd0", glyph: 5 }),
    });
  });

  it("throws the server's rejection reason", async () => {
    stubFetch({ ok: false, status: 400, json: () => Promise.resolve({ detail: "glyph out of range" }) });

    await expect(updateProjectSettings("website-redesign", "Website", "#2f6fd0", 42)).rejects.toThrow(
      "glyph out of range",
    );
  });
});

describe("deleteProjectRequest", () => {
  it("posts to the project's delete endpoint", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ fallback_id: EVERYTHING_PROJECT_ID }) });

    await deleteProjectRequest("taxes");

    expect(mockFetch).toHaveBeenCalledWith("/api/projects/taxes/delete", { method: "POST" });
  });

  it("throws the server's refusal to delete Everything", async () => {
    stubFetch({
      ok: false,
      status: 400,
      json: () => Promise.resolve({ detail: "the Everything project cannot be deleted" }),
    });

    await expect(deleteProjectRequest(EVERYTHING_PROJECT_ID)).rejects.toThrow(
      "the Everything project cannot be deleted",
    );
  });
});
