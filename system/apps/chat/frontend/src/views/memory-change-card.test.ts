// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";
import type { MemoryChange, MemoryChangeCardHandlers, MemoryChangeCardState } from "./memory-change-card";
import { lineDiff, MemoryChangeCard, memoryChangeCache, renderMemoryChangeCard } from "./memory-change-card";

const CHANGE: MemoryChange = {
  id: "c1",
  time: "2026-11-18T22:00:00+00:00",
  summary: "added note 'tea' and its index line",
  chat_id: "chat-1",
  tool_use_ids: ["t1", "t2"],
  files: [
    {
      name: "MEMORY.md",
      before: "# Index\n- [A](a.md)",
      after: "# Index\n- [A](a.md)\n- [Tea](tea.md)",
      label: "Your index (the list of notes I read at the start of every chat)",
      before_text: "Index\nA",
      after_text: "Index\nA\nTea — likes tea",
      note_id: "",
      details_url: null,
    },
    {
      name: "tea.md",
      before: null,
      after: "---\nname: tea\n---\nlikes tea",
      label: "New note: Tea",
      before_text: null,
      after_text: "Summary: Likes tea\n\nThe user likes tea.",
      note_id: "tea",
      details_url: "http://memory-abc.localhost/?note=tea",
    },
  ],
  undone_at: null,
  details_url: "http://memory-abc.localhost/?note=tea",
};

function draw(state: Partial<MemoryChangeCardState>, handlers: Partial<MemoryChangeCardHandlers> = {}): HTMLElement {
  const root = document.createElement("div");
  const full: MemoryChangeCardState = {
    label: "Memory changed",
    summary: CHANGE.summary,
    change: CHANGE,
    isExpanded: false,
    isBusy: false,
    error: "",
    confirmDetail: null,
    ...state,
  };
  m.render(
    root,
    renderMemoryChangeCard(full, {
      onToggle: vi.fn(),
      onUndo: vi.fn(),
      onUndoAnyway: vi.fn(),
      onKeep: vi.fn(),
      ...handlers,
    }),
  );
  return root;
}

/** The roots the live tests mounted, unmounted after each test so a failed assertion leaves no fetch running. */
const mountedRoots: HTMLElement[] = [];

afterEach(() => {
  for (const root of mountedRoots.splice(0)) m.mount(root, null);
  memoryChangeCache.clearForTesting();
  vi.unstubAllGlobals();
});

describe("lineDiff", () => {
  it("marks added and removed lines and folds distant unchanged ones", () => {
    const before = ["a", "b", "c", "d", "e"].join("\n");
    const after = ["a", "b", "c", "d", "E"].join("\n");
    expect(lineDiff(before, after)).toEqual([
      ["fold", "3 unchanged lines"],
      ["same", "d"],
      ["removed", "e"],
      ["added", "E"],
    ]);
  });

  it("shows a new file as all added and a deleted one as all removed", () => {
    expect(lineDiff(null, "x\ny")).toEqual([
      ["added", "x"],
      ["added", "y"],
    ]);
    expect(lineDiff("x", null)).toEqual([["removed", "x"]]);
  });
});

describe("renderMemoryChangeCard", () => {
  it("names the change and offers Undo, with the lines hidden until asked for", () => {
    const root = draw({});
    expect(root.textContent).toContain("Memory changed: added note 'tea' and its index line");
    expect(root.querySelector(".memory-change-undo")).not.toBeNull();
    expect(root.querySelector(".memory-change-diff")).toBeNull();
  });

  it("shows each file's changed lines as a person reads them, without the note's bookkeeping", () => {
    const root = draw({ isExpanded: true });
    const files = [...root.querySelectorAll(".memory-change-file")].map((file) => file.textContent ?? "");
    expect(files[0]).toContain("Your index");
    expect(files[0]).toContain("Tea — likes tea");
    expect(files[1]).toContain("New note: Tea");
    expect(files[1]).toContain("Summary: Likes tea");
    expect(files[1]).toContain("The user likes tea.");
    const added = [
      ...root.querySelectorAll(".memory-change-file")[1].querySelectorAll(".memory-change-line--added"),
    ].map((line) => line.textContent);
    expect(added).toEqual(["Summary: Likes tea", "\u00a0", "The user likes tea."]);
    expect(files[1]).not.toContain("name:");
  });

  it("links to the note in the Memory app", () => {
    const link = draw({}).querySelector(".memory-change-details") as HTMLAnchorElement;
    expect(link.textContent).toBe("View details");
    expect(link.getAttribute("href")).toBe("http://memory-abc.localhost/?note=tea");
  });

  it("calls Undo when clicked", () => {
    const onUndo = vi.fn();
    const root = draw({}, { onUndo });
    (root.querySelector(".memory-change-undo") as HTMLButtonElement).click();
    expect(onUndo).toHaveBeenCalledOnce();
  });

  it("drops Undo once the change is undone", () => {
    const root = draw({ change: { ...CHANGE, undone_at: "2026-11-18T22:05:00+00:00" } });
    expect(root.textContent).toContain("Memory change undone:");
    expect(root.querySelector(".memory-change-card--undone")).not.toBeNull();
    expect(root.querySelector(".memory-change-undo")).toBeNull();
  });

  it("opens with the notice's label from the backend", () => {
    const root = draw({ label: "Notiz geändert" });
    expect(root.textContent).toContain("Notiz geändert: added note 'tea' and its index line");
  });

  it("asks whether to undo anyway in place of Undo, while a refusal waits on the user", () => {
    const onUndoAnyway = vi.fn();
    const onKeep = vi.fn();
    const root = draw({ confirmDetail: "Changed again since: the index." }, { onUndoAnyway, onKeep });
    expect(root.querySelector(".memory-change-confirm")?.textContent).toContain("Changed again since: the index.");
    expect(root.querySelector(".memory-change-undo")).toBeNull();
    expect(root.querySelector(".memory-change-undo-anyway")?.getAttribute("data-variant")).toBe("destructive");
    (root.querySelector(".memory-change-undo-anyway") as HTMLButtonElement).click();
    (root.querySelector(".memory-change-keep") as HTMLButtonElement).click();
    expect(onUndoAnyway).toHaveBeenCalledOnce();
    expect(onKeep).toHaveBeenCalledOnce();
  });

  it("shows the notice's summary before the change has loaded, with no actions yet", () => {
    const root = draw({ change: null });
    expect(root.textContent).toContain("Memory changed: added note 'tea' and its index line");
    expect(root.querySelector(".memory-change-undo")).toBeNull();
  });
});

describe("MemoryChangeCard", () => {
  const REFUSAL = "Changed again since: the index. Undoing now would also lose those later edits.";

  function jsonResponse(body: unknown, status: number): Response {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }

  /** A fetch that answers each call with the next of ``answers``; an Error is thrown, as a network failure is. */
  function fetchAnswering(...answers: (Response | Promise<Response> | Error)[]): Mock<typeof fetch> {
    const queue = [...answers];
    return vi.fn<typeof fetch>(async () => {
      const answer = queue.shift();
      if (answer === undefined) throw new Error("fetch called more often than the test expects");
      if (answer instanceof Error) throw answer;
      return answer;
    });
  }

  function mountCard(): HTMLElement {
    const root = document.createElement("div");
    m.mount(root, {
      view: () => m(MemoryChangeCard, { changeId: "c1", label: "Memory changed", summary: CHANGE.summary }),
    });
    mountedRoots.push(root);
    return root;
  }

  /** Redraw until ``isReady`` holds: the card redraws itself only once its fetch has answered. */
  async function settle(isReady: () => boolean): Promise<void> {
    await vi.waitFor(() => {
      m.redraw.sync();
      expect(isReady()).toBe(true);
    });
  }

  function click(root: HTMLElement, selector: string): void {
    (root.querySelector(selector) as HTMLButtonElement).click();
  }

  function postedBodies(fetchMock: Mock<typeof fetch>): unknown[] {
    return fetchMock.mock.calls
      .map(([, init]) => init)
      .filter((init) => init?.method === "POST")
      .map((init) => JSON.parse(String(init?.body)));
  }

  it("says why the change could not be loaded, with no Undo", async () => {
    vi.stubGlobal("fetch", fetchAnswering(jsonResponse({ detail: "That memory change is not in the record." }, 404)));
    const root = mountCard();
    await settle(() => root.querySelector(".memory-change-error") !== null);
    expect(root.querySelector(".memory-change-error")?.textContent).toBe("That memory change is not in the record.");
    expect(root.querySelector(".memory-change-undo")).toBeNull();
  });

  it("asks in the card when a file changed again since, and undoes anyway when told to", async () => {
    const fetchMock = fetchAnswering(
      jsonResponse(CHANGE, 200),
      jsonResponse({ detail: REFUSAL }, 409),
      jsonResponse({ ...CHANGE, undone_at: "2026-11-18T22:05:00Z" }, 200),
    );
    vi.stubGlobal("fetch", fetchMock);
    const root = mountCard();
    await settle(() => root.querySelector(".memory-change-undo") !== null);

    click(root, ".memory-change-undo");
    await settle(() => root.querySelector(".memory-change-confirm") !== null);
    expect(root.querySelector(".memory-change-confirm")?.textContent).toContain(REFUSAL);
    expect(root.querySelector(".memory-change-undo")).toBeNull();

    click(root, ".memory-change-undo-anyway");
    await settle(() => (root.textContent ?? "").includes("Memory change undone:"));
    expect(root.querySelector(".memory-change-confirm")).toBeNull();
    expect(postedBodies(fetchMock)).toEqual([{ is_forced: false }, { is_forced: true }]);
    expect(String(fetchMock.mock.calls[2][0])).toContain("/api/memory-changes/c1/undo");
  });

  it("keeps everything as it is when told to keep it", async () => {
    const fetchMock = fetchAnswering(jsonResponse(CHANGE, 200), jsonResponse({ detail: REFUSAL }, 409));
    vi.stubGlobal("fetch", fetchMock);
    const root = mountCard();
    await settle(() => root.querySelector(".memory-change-undo") !== null);
    click(root, ".memory-change-undo");
    await settle(() => root.querySelector(".memory-change-keep") !== null);

    click(root, ".memory-change-keep");
    await settle(() => root.querySelector(".memory-change-confirm") === null);
    expect(root.querySelector(".memory-change-undo")).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("disables Undo while the undo is in flight", async () => {
    let answerUndo: (response: Response) => void = () => {};
    const pendingUndo = new Promise<Response>((resolve) => {
      answerUndo = resolve;
    });
    vi.stubGlobal("fetch", fetchAnswering(jsonResponse(CHANGE, 200), pendingUndo));
    const root = mountCard();
    await settle(() => root.querySelector(".memory-change-undo") !== null);

    click(root, ".memory-change-undo");
    await settle(() => (root.querySelector(".memory-change-undo") as HTMLButtonElement | null)?.disabled === true);

    answerUndo(jsonResponse({ ...CHANGE, undone_at: "2026-11-18T22:05:00Z" }, 200));
    await settle(() => root.querySelector(".memory-change-undo") === null);
    expect(root.textContent).toContain("Memory change undone:");
  });

  it("says when the chat app cannot be reached", async () => {
    vi.stubGlobal("fetch", fetchAnswering(jsonResponse(CHANGE, 200), new TypeError("Failed to fetch")));
    const root = mountCard();
    await settle(() => root.querySelector(".memory-change-undo") !== null);
    click(root, ".memory-change-undo");
    await settle(() => root.querySelector(".memory-change-error") !== null);
    expect(root.querySelector(".memory-change-error")?.textContent).toBe("The chat app could not be reached.");
    expect((root.querySelector(".memory-change-undo") as HTMLButtonElement).disabled).toBe(false);
  });

  it("keeps what it loaded, what was undone and whether it was open when remounted", async () => {
    const fetchMock = fetchAnswering(
      jsonResponse(CHANGE, 200),
      jsonResponse({ ...CHANGE, undone_at: "2026-11-18T22:05:00Z" }, 200),
    );
    vi.stubGlobal("fetch", fetchMock);
    const first = mountCard();
    await settle(() => first.querySelector(".memory-change-undo") !== null);
    click(first, ".memory-change-toggle");
    click(first, ".memory-change-undo");
    await settle(() => (first.textContent ?? "").includes("Memory change undone:"));
    m.mount(first, null);

    const second = mountCard();
    m.redraw.sync();
    expect(second.textContent).toContain("Memory change undone:");
    expect(second.querySelector(".memory-change-diff")).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
