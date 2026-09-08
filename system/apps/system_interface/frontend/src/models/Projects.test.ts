import { afterEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  EVERYTHING_VIEW_ID,
  addMember,
  appInstanceRef,
  appShortcutId,
  autosaveProject,
  buildEverythingMembers,
  chatAgentIdFromRef,
  chooseInitialViewId,
  createProject,
  deleteProjectRequest,
  fetchMemberMap,
  fetchProjectContent,
  fetchProjectsList,
  defaultShortcutMode,
  instanceNameFromRef,
  instanceNumberFromName,
  filingProjectForAgentOp,
  isEverythingView,
  isShortcutPinned,
  memberKindFromRef,
  memberRef,
  partitionByMembership,
  projectForViewId,
  removeMember,
  shortcutModeForProject,
  removePanelFromAllProjects,
  searchMembers,
  serviceNameFromInstanceName,
  serviceNameFromRef,
  shareMember,
  updateProjectSettings,
  type MachineInventory,
  type MemberKind,
  type ProjectInfo,
} from "./Projects";

const WEBSITE: ProjectInfo = {
  project_id: "website-redesign",
  name: "Website Redesign",
  color: "#4f8ef7",
  glyph: 3,
  has_content: true,
  members: ["service:web", "terminal:build"],
};
const TAXES: ProjectInfo = {
  project_id: "taxes",
  name: "Taxes",
  color: "#e5a33d",
  glyph: 7,
  has_content: false,
  members: [],
};

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

/** One searchable row, with only the fields the search cares about. */
function row(label: string, kind: MemberKind): { label: string; kind: MemberKind } {
  return { label, kind };
}

/** A machine holding nothing, to be spread over with the one kind under test. */
const EMPTY_INVENTORY: MachineInventory = {
  chatAgents: [],
  terminals: [],
  browsers: [],
  appInstances: [],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("isEverythingView", () => {
  it("tells the unfiltered view apart from a project", () => {
    expect(isEverythingView(EVERYTHING_VIEW_ID)).toBe(true);
    expect(isEverythingView("website-redesign")).toBe(false);
    expect(isEverythingView("")).toBe(false);
  });
});

describe("chooseInitialViewId", () => {
  it("prefers the browser's stored choice when it still exists", () => {
    expect(chooseInitialViewId([WEBSITE, TAXES], "taxes")).toBe("taxes");
  });

  it("falls back to the first project when the stored choice is gone", () => {
    expect(chooseInitialViewId([WEBSITE, TAXES], "deleted-project")).toBe("website-redesign");
  });

  it("picks the first project on a first-ever connect", () => {
    expect(chooseInitialViewId([WEBSITE, TAXES], "")).toBe("website-redesign");
  });

  it("keeps a client that was viewing Everything on Everything", () => {
    // Everything is the home and has a layout of its own, so there is nothing
    // to fall back from -- and it is never in the project list to be found.
    expect(chooseInitialViewId([WEBSITE, TAXES], EVERYTHING_VIEW_ID)).toBe(EVERYTHING_VIEW_ID);
  });

  it("still lands on Everything when the registry could not be read", () => {
    expect(chooseInitialViewId([], EVERYTHING_VIEW_ID)).toBe(EVERYTHING_VIEW_ID);
  });

  it("lands on Everything when no projects exist and none was stored", () => {
    // A machine may genuinely have zero projects now that deleting one is a
    // pure view operation, so this is no longer treated as an unreadable
    // registry -- Everything is always there to land on.
    expect(chooseInitialViewId([], "anything")).toBe(EVERYTHING_VIEW_ID);
  });
});

describe("projectForViewId", () => {
  it("resolves a project id to its registry entry", () => {
    expect(projectForViewId([WEBSITE, TAXES], "taxes")).toBe(TAXES);
  });

  it("resolves Everything to nothing, since no project backs it", () => {
    expect(projectForViewId([WEBSITE, TAXES], EVERYTHING_VIEW_ID)).toBeNull();
  });

  it("resolves a deleted project to nothing", () => {
    expect(projectForViewId([WEBSITE], "taxes")).toBeNull();
  });
});

describe("fetchProjectsList", () => {
  it("returns the registry the server sent", async () => {
    const mockFetch = stubFetch({
      ok: true,
      json: () => Promise.resolve({ projects: [WEBSITE, TAXES], last_active_id: "website-redesign" }),
    });

    expect(await fetchProjectsList()).toEqual({
      projects: [WEBSITE, TAXES],
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
  // The device rides along from getDeviceKind(); node's own navigator reads as desktop.
  it("returns the saved content and percent-encodes the id", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ layout: { dockview: { grid: {} } } }) });

    expect(await fetchProjectContent("my project")).toEqual({ dockview: { grid: {} } });
    expect(mockFetch).toHaveBeenCalledWith("/api/projects/my%20project?device=desktop");
  });

  it("fetches Everything's own layout like any other view's", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ layout: { dockview: { grid: {} } } }) });

    expect(await fetchProjectContent(EVERYTHING_VIEW_ID)).toEqual({ dockview: { grid: {} } });
    expect(mockFetch).toHaveBeenCalledWith("/api/projects/everything?device=desktop");
  });

  it("returns null for a view that has never been saved", async () => {
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
  it("posts the content under the active view and client id", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({}) });

    await autosaveProject("website-redesign", { dockview: { grid: {} } }, "client-7");

    expect(mockFetch).toHaveBeenCalledWith("/api/projects/website-redesign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ layout: { dockview: { grid: {} } }, client_id: "client-7", device: "desktop" }),
    });
  });

  it("autosaves Everything's own layout under its view id", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({}) });

    await autosaveProject(EVERYTHING_VIEW_ID, { dockview: {} }, "client-7");

    expect(mockFetch).toHaveBeenCalledWith("/api/projects/everything", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ layout: { dockview: {} }, client_id: "client-7", device: "desktop" }),
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

  it("says the workspace is unreachable rather than showing a bare gateway status", async () => {
    // A 502/503/504 comes from the tunnel in FRONT of the server -- a workspace
    // provisioning, restarting or shutting down -- so the request reached no
    // endpoint and nothing changed. "HTTP 503" read as a bug in whatever the
    // user had just clicked.
    for (const status of [502, 503, 504]) {
      stubFetch({ ok: false, status, json: () => Promise.reject(new Error("not json")) });
      await expect(createProject("Taxes", "#e5a33d", 7)).rejects.toThrow(/not responding right now/);
    }
  });

  it("still shows a bare status for one it has nothing better to say about", async () => {
    stubFetch({ ok: false, status: 418, json: () => Promise.reject(new Error("not json")) });
    await expect(createProject("Taxes", "#e5a33d", 7)).rejects.toThrow("HTTP 418");
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
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ fallback_id: "website-redesign" }) });

    await deleteProjectRequest("taxes");

    expect(mockFetch).toHaveBeenCalledWith("/api/projects/taxes/delete", { method: "POST" });
  });

  it("throws the server's rejection reason for an unknown project", async () => {
    stubFetch({ ok: false, status: 404, json: () => Promise.resolve({ detail: "Project 'gone' not found" }) });

    await expect(deleteProjectRequest("gone")).rejects.toThrow("Project 'gone' not found");
  });
});

describe("removePanelFromAllProjects", () => {
  it("posts the destroyed panel with the member it stood for", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ project_ids: ["taxes"] }) });

    expect(await removePanelFromAllProjects("terminal-session-build", "terminal:build")).toEqual(["taxes"]);
    expect(mockFetch).toHaveBeenCalledWith("/api/projects/panels/terminal-session-build/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "terminal:build" }),
    });
  });

  it("sends a null ref when the caller knows only the panel", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({}) });

    expect(await removePanelFromAllProjects("iframe-web-17")).toEqual([]);
    expect(mockFetch).toHaveBeenCalledWith("/api/projects/panels/iframe-web-17/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: null }),
    });
  });

  it("throws the server's rejection reason", async () => {
    stubFetch({ ok: false, status: 500, json: () => Promise.resolve({ detail: "no primary agent" }) });

    await expect(removePanelFromAllProjects("chat-a1")).rejects.toThrow("no primary agent");
  });
});

describe("addMember", () => {
  it("posts the ref to the project's members endpoint", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ status: "ok" }) });

    await addMember("website redesign", "service:browser?session=2");

    expect(mockFetch).toHaveBeenCalledWith("/api/projects/website%20redesign/members", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "service:browser?session=2" }),
    });
  });

  it("throws the server's rejection reason", async () => {
    // Only an unknown project is a rejection: another project already showing
    // the ref is ordinary, since a project is a view rather than an owner.
    stubFetch({ ok: false, status: 404, json: () => Promise.resolve({ detail: "Project 'gone' not found" }) });

    await expect(addMember("gone", "service:web")).rejects.toThrow("Project 'gone' not found");
  });
});

describe("removeMember", () => {
  it("posts the ref to the project's member-removal endpoint", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ status: "ok" }) });

    await removeMember("taxes", "terminal:build");

    expect(mockFetch).toHaveBeenCalledWith("/api/projects/taxes/members/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "terminal:build" }),
    });
  });

  it("throws the server's rejection reason", async () => {
    stubFetch({ ok: false, status: 404, json: () => Promise.resolve({ detail: "Project 'gone' not found" }) });

    await expect(removeMember("gone", "terminal:build")).rejects.toThrow("Project 'gone' not found");
  });
});

describe("shareMember", () => {
  it("returns every project showing the ref once the destination has it too", async () => {
    const mockFetch = stubFetch({
      ok: true,
      json: () =>
        Promise.resolve({ ref: "service:web", to_project_id: "taxes", projects: ["website-redesign", "taxes"] }),
    });

    expect(await shareMember("service:web", "taxes")).toEqual(["website-redesign", "taxes"]);
    expect(mockFetch).toHaveBeenCalledWith("/api/projects/members/share", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "service:web", to_project_id: "taxes" }),
    });
  });

  it("takes the ref from nowhere, so the source keeps showing it", async () => {
    // The launcher's "on this machine" open: sharing adds and never moves.
    stubFetch({ ok: true, json: () => Promise.resolve({ projects: ["website-redesign", "taxes"] }) });

    expect(await shareMember("service:web", "taxes")).toContain("website-redesign");
  });

  it("tolerates a response missing the project list", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({}) });

    expect(await shareMember("chat:a1", "taxes")).toEqual([]);
  });

  it("throws the server's rejection reason", async () => {
    stubFetch({ ok: false, status: 404, json: () => Promise.resolve({ detail: "Project 'gone' not found" }) });

    await expect(shareMember("chat:a1", "gone")).rejects.toThrow("Project 'gone' not found");
  });
});

describe("fetchMemberMap", () => {
  it("returns the machine-wide ref to showing-projects map", async () => {
    const mockFetch = stubFetch({
      ok: true,
      json: () => Promise.resolve({ members: { "service:web": ["website-redesign", "taxes"], "chat:a1": ["taxes"] } }),
    });

    expect(await fetchMemberMap()).toEqual({ "service:web": ["website-redesign", "taxes"], "chat:a1": ["taxes"] });
    expect(mockFetch).toHaveBeenCalledWith("/api/projects/members");
  });

  it("yields an empty map when the server rejects the read or is unreachable", async () => {
    stubFetch({ ok: false, status: 500, json: () => Promise.resolve({}) });
    expect(await fetchMemberMap()).toEqual({});

    stubUnreachableFetch();
    expect(await fetchMemberMap()).toEqual({});
  });

  it("tolerates a response missing the map", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({}) });

    expect(await fetchMemberMap()).toEqual({});
  });
});

describe("memberKindFromRef", () => {
  it("classifies each form of the store's ref grammar", () => {
    expect(memberKindFromRef("chat:a1b2c3")).toBe("chat");
    expect(memberKindFromRef("terminal:build")).toBe("terminal");
    expect(memberKindFromRef("url:9f86d081")).toBe("url");
    expect(memberKindFromRef("service:web")).toBe("app");
  });

  it("reads the browser fleet as browsers, with or without a session", () => {
    expect(memberKindFromRef("service:browser?session=2")).toBe("browser");
    expect(memberKindFromRef("service:browser")).toBe("browser");
  });

  it("reads the terminal service as a terminal", () => {
    expect(memberKindFromRef("service:terminal")).toBe("terminal");
  });

  it("does not mistake an app whose name merely starts with a fleet name", () => {
    expect(memberKindFromRef("service:browser-tests")).toBe("app");
  });

  it("falls back to the generic app row for anything unrecognized", () => {
    expect(memberKindFromRef("nonsense")).toBe("app");
    expect(memberKindFromRef("")).toBe("app");
  });
});

describe("memberRef", () => {
  it("builds each form of the store's ref grammar", () => {
    expect(memberRef("chat", "a1b2c3")).toBe("chat:a1b2c3");
    expect(memberRef("terminal", "build")).toBe("terminal:build");
    expect(memberRef("url", "9f86d081")).toBe("url:9f86d081");
    expect(memberRef("app", "web")).toBe("service:web");
  });

  it("addresses a fleet browser by its session, so two browsers stay distinct", () => {
    expect(memberRef("browser", "quiet-otter")).toBe("service:browser?session=quiet-otter");
    expect(memberRef("browser", "loud-otter")).not.toBe(memberRef("browser", "quiet-otter"));
  });

  it("round-trips through memberKindFromRef for every kind", () => {
    const kinds: MemberKind[] = ["chat", "terminal", "url", "browser", "app"];

    for (const kind of kinds) {
      expect(memberKindFromRef(memberRef(kind, "thing"))).toBe(kind);
    }
  });
});

describe("chatAgentIdFromRef", () => {
  it("recovers the agent id a chat ref was built from", () => {
    expect(chatAgentIdFromRef(memberRef("chat", "agent-9"))).toBe("agent-9");
  });

  it("answers null for a ref that addresses no chat", () => {
    expect(chatAgentIdFromRef("terminal:build")).toBeNull();
    expect(chatAgentIdFromRef("chat:")).toBeNull();
  });
});

describe("serviceNameFromRef", () => {
  it("recovers the name an app ref was built from", () => {
    expect(serviceNameFromRef(memberRef("app", "web"))).toBe("web");
    expect(serviceNameFromRef("service:notes")).toBe("notes");
  });

  it("answers null for a ref that addresses no installed app", () => {
    expect(serviceNameFromRef("chat:a1b2c3")).toBeNull();
    expect(serviceNameFromRef("terminal:build")).toBeNull();
    expect(serviceNameFromRef("url:9f86d081")).toBeNull();
    expect(serviceNameFromRef("nonsense")).toBeNull();
    expect(serviceNameFromRef("")).toBeNull();
    expect(serviceNameFromRef("service:")).toBeNull();
  });

  it("answers null for a fleet browser, which is a session rather than an app", () => {
    expect(serviceNameFromRef(memberRef("browser", "quiet-otter"))).toBeNull();
  });

  it("answers an instance ref with its service: the instance is a page of it", () => {
    expect(serviceNameFromRef("service:files?instance=files-2")).toBe("files");
  });
});

describe("app instance refs", () => {
  it("builds and parses the instance ref round trip", () => {
    const ref = appInstanceRef("files", "files-2");
    expect(ref).toBe("service:files?instance=files-2");
    expect(instanceNameFromRef(ref)).toBe("files-2");
    expect(memberKindFromRef(ref)).toBe("app");
  });

  it("reads no instance out of a bare service ref or a browser session ref", () => {
    expect(instanceNameFromRef("service:files")).toBeNull();
    expect(instanceNameFromRef(memberRef("browser", "browser-2"))).toBeNull();
    expect(instanceNameFromRef("chat:a1")).toBeNull();
  });

  it("parses the canonical instance name, digits-ending services included", () => {
    expect(instanceNumberFromName("files-2")).toBe(2);
    expect(serviceNameFromInstanceName("files-2")).toBe("files");
    expect(serviceNameFromInstanceName("app-2-3")).toBe("app-2");
    expect(instanceNumberFromName("files")).toBeNull();
    expect(serviceNameFromInstanceName("files-0")).toBeNull();
  });
});

describe("buildEverythingMembers", () => {
  it("enumerates the machine kind by kind, in inventory order", () => {
    const inventory: MachineInventory = {
      chatAgents: [{ name: "a1", label: "Planning" }],
      terminals: [{ name: "build", label: "build" }],
      browsers: [{ name: "quiet-otter", label: "Browser quiet-otter" }],
      appInstances: [{ serviceName: "web", instanceName: "web-1", label: "web 1" }],
    };

    expect(buildEverythingMembers(inventory, {})).toEqual([
      { ref: "chat:a1", kind: "chat", label: "Planning", projectIds: [] },
      { ref: "terminal:build", kind: "terminal", label: "build", projectIds: [] },
      { ref: "service:browser?session=quiet-otter", kind: "browser", label: "Browser quiet-otter", projectIds: [] },
      { ref: "service:web?instance=web-1", kind: "app", label: "web 1", projectIds: [] },
    ]);
  });

  it("lists an object filed in no project at all", () => {
    // The whole reason Everything enumerates the machine instead of unioning
    // member lists: a side chat nobody filed anywhere is still on the machine.
    const rows = buildEverythingMembers(
      { ...EMPTY_INVENTORY, chatAgents: [{ name: "loose", label: "Side chat" }] },
      { "service:web": ["website-redesign"] },
    );

    expect(rows).toEqual([{ ref: "chat:loose", kind: "chat", label: "Side chat", projectIds: [] }]);
  });

  it("decorates a row with every project showing it", () => {
    const rows = buildEverythingMembers(
      { ...EMPTY_INVENTORY, appInstances: [{ serviceName: "web", instanceName: "web-1", label: "web 1" }] },
      { "service:web?instance=web-1": ["website-redesign", "taxes"] },
    );

    expect(rows[0].projectIds).toEqual(["website-redesign", "taxes"]);
  });

  it("copies the project list rather than aliasing the map", () => {
    const projectsByRef = { "service:web?instance=web-1": ["taxes"] };
    const rows = buildEverythingMembers(
      { ...EMPTY_INVENTORY, appInstances: [{ serviceName: "web", instanceName: "web-1", label: "web 1" }] },
      projectsByRef,
    );

    rows[0].projectIds.push("website-redesign");

    expect(projectsByRef["service:web?instance=web-1"]).toEqual(["taxes"]);
  });

  it("keeps the order each source listed its objects in", () => {
    const rows = buildEverythingMembers(
      {
        ...EMPTY_INVENTORY,
        terminals: [
          { name: "build", label: "build" },
          { name: "logs", label: "logs" },
        ],
      },
      {},
    );

    expect(rows.map((memberRow) => memberRow.ref)).toEqual(["terminal:build", "terminal:logs"]);
  });

  it("collapses a duplicate onto the first row for it", () => {
    // A source that reports the same object twice -- a fleet listing mid-
    // refresh, say -- must not make Everything list it twice.
    const rows = buildEverythingMembers(
      {
        ...EMPTY_INVENTORY,
        terminals: [
          { name: "build", label: "build" },
          { name: "build", label: "build (again)" },
        ],
      },
      {},
    );

    expect(rows).toEqual([{ ref: "terminal:build", kind: "terminal", label: "build", projectIds: [] }]);
  });

  it("skips an object the machine reported with no name", () => {
    expect(
      buildEverythingMembers(
        { ...EMPTY_INVENTORY, appInstances: [{ serviceName: "", instanceName: "", label: "unnamed" }] },
        {},
      ),
    ).toEqual([]);
  });

  it("yields nothing for an empty machine", () => {
    expect(buildEverythingMembers(EMPTY_INVENTORY, { "service:web": ["taxes"] })).toEqual([]);
  });
});

describe("partitionByMembership", () => {
  const OBJECTS = [
    { ref: "service:web", label: "web" },
    { ref: "chat:a1", label: "Planning" },
    { ref: "terminal:build", label: "build" },
  ];

  it("splits the machine's objects by what this project shows", () => {
    expect(partitionByMembership(OBJECTS, ["service:web", "terminal:build"])).toEqual({
      inProject: [OBJECTS[0], OBJECTS[2]],
      onMachine: [OBJECTS[1]],
    });
  });

  it("puts everything on the machine side for a project with no members", () => {
    expect(partitionByMembership(OBJECTS, [])).toEqual({ inProject: [], onMachine: OBJECTS });
  });

  it("leaves an object other projects show on the machine side", () => {
    // Membership is many-to-many, so the other project holding it says nothing
    // about this one: opening it here adds it and takes it from nowhere.
    const { onMachine } = partitionByMembership(OBJECTS, ["chat:a1"]);

    expect(onMachine.map((object) => object.ref)).toEqual(["service:web", "terminal:build"]);
  });

  it("ignores a member ref for something the machine no longer holds", () => {
    expect(partitionByMembership(OBJECTS, ["terminal:gone"])).toEqual({ inProject: [], onMachine: OBJECTS });
  });

  it("preserves input order within each half and hands rows back by identity", () => {
    const { inProject } = partitionByMembership(OBJECTS, ["terminal:build", "service:web"]);

    expect(inProject[0]).toBe(OBJECTS[0]);
    expect(inProject[1]).toBe(OBJECTS[2]);
  });
});

describe("searchMembers", () => {
  const ROWS = [row("Website", "app"), row("Docs", "browser"), row("build", "terminal"), row("Planning chat", "chat")];

  it("keeps every row, unbolded, for an empty or blank query", () => {
    expect(searchMembers(ROWS, "")).toEqual(ROWS.map((member) => ({ member, labelRanges: [] })));
    expect(searchMembers(ROWS, "   ")).toEqual(ROWS.map((member) => ({ member, labelRanges: [] })));
  });

  it("matches the label case-insensitively and reports where it hit", () => {
    expect(searchMembers(ROWS, "web")).toEqual([{ member: ROWS[0], labelRanges: [{ start: 0, end: 3 }] }]);
  });

  it("keeps a row on its kind alone, with nothing to bold", () => {
    // "browser" keeps browsers however the tab is titled.
    expect(searchMembers(ROWS, "browser")).toEqual([{ member: ROWS[1], labelRanges: [] }]);
  });

  it("bolds the label when the query matches both label and kind", () => {
    expect(searchMembers(ROWS, "chat")).toEqual([{ member: ROWS[3], labelRanges: [{ start: 9, end: 13 }] }]);
  });

  it("drops rows that match neither label nor kind", () => {
    expect(searchMembers(ROWS, "zzz")).toEqual([]);
  });

  it("preserves the input order of the rows it keeps", () => {
    const results = searchMembers(ROWS, "b");

    expect(results.map((result) => result.member.label)).toEqual(["Website", "Docs", "build"]);
  });

  it("reports every occurrence in the label, left to right and non-overlapping", () => {
    const banana = [row("banana", "app")];

    expect(searchMembers(banana, "an")).toEqual([
      {
        member: banana[0],
        labelRanges: [
          { start: 1, end: 3 },
          { start: 3, end: 5 },
        ],
      },
    ]);
  });

  it("does not let overlapping occurrences double-count", () => {
    const aaaa = [row("aaaa", "app")];

    expect(searchMembers(aaaa, "aa")).toEqual([
      {
        member: aaaa[0],
        labelRanges: [
          { start: 0, end: 2 },
          { start: 2, end: 4 },
        ],
      },
    ]);
  });

  it("indexes ranges into the original label, not a lowercased copy", () => {
    const rows = [row("Browser Session", "browser")];
    const [result] = searchMembers(rows, "ses");

    expect(result.labelRanges).toEqual([{ start: 8, end: 11 }]);
    expect(rows[0].label.substring(8, 11)).toBe("Ses");
  });

  it("trims the query before matching", () => {
    expect(searchMembers(ROWS, "  docs  ")).toEqual([{ member: ROWS[1], labelRanges: [{ start: 0, end: 4 }] }]);
  });

  it("treats the query as a literal substring, not a pattern", () => {
    const dotted = [row("a.b", "app"), row("axb", "app")];

    expect(searchMembers(dotted, "a.b")).toEqual([{ member: dotted[0], labelRanges: [{ start: 0, end: 3 }] }]);
  });

  it("keeps nothing when the query is longer than every label and kind", () => {
    expect(searchMembers([row("Docs", "browser")], "documentation")).toEqual([]);
  });

  it("hands each row back by identity so callers keep their own fields", () => {
    const rows = [{ label: "Docs", kind: "browser" as const, ref: "service:browser?session=2" }];
    const [result] = searchMembers(rows, "docs");

    expect(result.member).toBe(rows[0]);
    expect(result.member.ref).toBe("service:browser?session=2");
  });
});

describe("filingProjectForAgentOp", () => {
  it("files into the requester's own project when it is registered", () => {
    expect(filingProjectForAgentOp("website-redesign", [WEBSITE])).toBe("website-redesign");
  });

  it("falls back when the requester has no project label or an unregistered one", () => {
    expect(filingProjectForAgentOp(null, [WEBSITE])).toBeNull();
    expect(filingProjectForAgentOp(undefined, [WEBSITE])).toBeNull();
    expect(filingProjectForAgentOp("", [WEBSITE])).toBeNull();
    // A label naming a project that no longer exists must not file anywhere.
    expect(filingProjectForAgentOp("deleted-project", [WEBSITE])).toBeNull();
  });
});

describe("shortcut overrides", () => {
  const base: ProjectInfo = {
    project_id: "p",
    name: "P",
    color: "#000000",
    glyph: 0,
    has_content: false,
    members: [],
  };

  it("keeps every shortcut pinned until an override says otherwise", () => {
    expect(isShortcutPinned(base, "terminal")).toBe(true);
    expect(isShortcutPinned({ ...base, shortcut_overrides: { terminal: { is_pinned: false } } }, "terminal")).toBe(
      false,
    );
    // A null field is the server spelling "unset", which means the default.
    expect(isShortcutPinned({ ...base, shortcut_overrides: { terminal: { is_pinned: null } } }, "terminal")).toBe(
      true,
    );
    // Everything (a null project) always shows the full set.
    expect(isShortcutPinned(null, "terminal")).toBe(true);
  });

  it("defaults chat to new mode and everything else to focus", () => {
    expect(defaultShortcutMode("chat")).toBe("new");
    expect(defaultShortcutMode("terminal")).toBe("focus");
    expect(defaultShortcutMode("app:docs")).toBe("focus");
  });

  it("reads a stored mode override and falls back to the default otherwise", () => {
    expect(shortcutModeForProject(base, "chat")).toBe("new");
    expect(shortcutModeForProject({ ...base, shortcut_overrides: { chat: { mode: "focus" } } }, "chat")).toBe("focus");
    expect(shortcutModeForProject({ ...base, shortcut_overrides: { "app:docs": { mode: "new" } } }, "app:docs")).toBe(
      "new",
    );
    // Junk from a hand-edited registry falls back rather than leaking through.
    expect(shortcutModeForProject({ ...base, shortcut_overrides: { chat: { mode: "sometimes" } } }, "chat")).toBe(
      "new",
    );
    // Everything runs the defaults.
    expect(shortcutModeForProject(null, "chat")).toBe("new");
  });

  it("builds an app's shortcut id from its service name", () => {
    expect(appShortcutId("docs")).toBe("app:docs");
  });
});
