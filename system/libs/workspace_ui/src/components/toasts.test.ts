// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

import type { AgentFixRequest, ReportedFailure } from "../app_contract";
import { ToastQueue } from "../models/toasts";
import { mountView, unmountViews } from "../testing/mount";
import { Toasts } from "./toasts";

const TOASTS = [
  {
    id: 1,
    message: "Could not move it",
    details: null,
    actions: [],
    isLasting: false,
    isHelpOwed: false,
    isAgentOffered: false,
    agentRequest: { whatFailed: "Could not move it", details: null },
    isBugReportOffered: false,
  },
  {
    id: 2,
    message: "Could not copy it",
    details: null,
    actions: [],
    isLasting: false,
    isHelpOwed: false,
    isAgentOffered: false,
    agentRequest: { whatFailed: "Could not copy it", details: null },
    isBugReportOffered: false,
  },
];

afterEach(() => {
  unmountViews();
});

function mountQueue(
  queue: ToastQueue,
  onReportBug: () => ((failure: ReportedFailure) => void) | null = () => null,
  onAskAgent: () => ((request: AgentFixRequest) => Promise<unknown>) | null = () => null,
): HTMLElement {
  return mountView(() =>
    m(Toasts, {
      toasts: queue.current(),
      placementClass: "fixed",
      onDismiss: (id) => queue.dismiss(id),
      onReportBug: onReportBug(),
      onAskAgent: onAskAgent(),
    }),
  );
}

describe("Toasts", () => {
  it("is a live region even while empty, so a toast added later is announced", () => {
    const root = document.createElement("div");
    m.render(root, m(Toasts, { toasts: [], placementClass: "fixed bottom-4" }));
    const region = root.querySelector('[role="status"]');
    expect(region?.getAttribute("aria-live")).toBe("polite");
    expect(region?.className).toContain("fixed bottom-4");
    expect(root.querySelectorAll("[data-toast]")).toHaveLength(0);
  });

  it("takes no press and has no close button on a plain toast unless the page asks for one", () => {
    const root = document.createElement("div");
    m.render(root, m(Toasts, { toasts: TOASTS, placementClass: "", onDismiss: vi.fn() }));
    const toasts = Array.from(root.querySelectorAll<HTMLElement>("[data-toast]"));
    expect(toasts.map((toast) => toast.textContent)).toEqual(["Could not move it", "Could not copy it"]);
    expect(toasts.every((toast) => !toast.className.includes("pointer-events-auto"))).toBe(true);
    expect(root.querySelector("[data-toast-dismiss]")).toBeNull();
  });

  it("gives each plain toast a close button that dismisses that toast where the page asks for one", () => {
    const root = document.createElement("div");
    const onDismiss = vi.fn();
    m.render(root, m(Toasts, { toasts: TOASTS, placementClass: "", onDismiss, closesPlainToasts: true }));
    const toasts = Array.from(root.querySelectorAll<HTMLElement>("[data-toast]"));
    expect(toasts.every((toast) => toast.className.includes("pointer-events-auto"))).toBe(true);
    toasts[1].querySelector<HTMLButtonElement>("[data-toast-dismiss]")!.click();
    expect(onDismiss).toHaveBeenCalledWith(2);
  });

  it("gives a lasting note a close control, its buttons, and its details, and takes it away once one is pressed", () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    const retry = vi.fn();
    queue.show("This window couldn't be closed. Try again.", {
      isLasting: true,
      details: "HTTP 500",
      actions: [{ label: "Try again", run: retry }],
    });
    queue.show("A second note", { isLasting: true, details: "HTTP 502" });
    const root = mountQueue(queue);

    const [first, second] = Array.from(root.querySelectorAll("[data-toast]")) as HTMLElement[];
    expect(first.querySelector(".toast-message")?.textContent).toBe("This window couldn't be closed. Try again.");
    expect(first.querySelector(".see-details pre")?.textContent).toBe("HTTP 500");
    (first.querySelector(".toast-action") as HTMLButtonElement).click();
    expect(retry).toHaveBeenCalledTimes(1);
    expect(queue.current().map((toast) => toast.message)).toEqual(["A second note"]);

    (second.querySelector(".toast-close") as HTMLButtonElement).click();
    expect(queue.current()).toEqual([]);
  });

  it("keeps a lasting note up when the button pressed hands the failure on", () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    const ask = vi.fn();
    const retry = vi.fn();
    queue.show("Docs couldn't open that.", {
      isLasting: true,
      actions: [
        { label: "Try again", run: retry },
        { label: "Ask an agent to fix this", run: ask, keepsToast: true },
      ],
    });
    const root = mountQueue(queue);

    const [tryAgain, askAgent] = Array.from(root.querySelectorAll<HTMLButtonElement>(".toast-action"));
    askAgent.click();
    expect(ask).toHaveBeenCalledTimes(1);
    expect(queue.current()).toHaveLength(1);
    tryAgain.click();
    expect(retry).toHaveBeenCalledTimes(1);
    expect(queue.current()).toEqual([]);
  });

  it("offers Report a bug on a note that owes help once the page can open one, unless withheld, and keeps the note up", () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    queue.show("This window couldn't be closed. Try again.", {
      isLasting: true,
      details: "HTTP 500",
      isHelpOwed: true,
    });
    queue.show("Docs couldn't open that.", { isLasting: true, details: "HTTP 400" });
    queue.show("Notes couldn't open that. Try again.", {
      isLasting: true,
      details: "HTTP 502",
      isHelpOwed: true,
      isBugReportWithheld: true,
    });
    let reportBug: ((failure: ReportedFailure) => void) | null = null;
    const root = mountQueue(queue, () => reportBug);
    expect(root.querySelector(".report-bug")).toBeNull();

    const reported: ReportedFailure[] = [];
    reportBug = (failure) => void reported.push(failure);
    m.redraw.sync();
    const buttons = Array.from(root.querySelectorAll<HTMLButtonElement>(".report-bug"));
    expect(buttons.map((button) => button.textContent)).toEqual(["Report a bug"]);
    buttons[0].click();
    expect(reported).toEqual([{ message: "This window couldn't be closed. Try again.", details: "HTTP 500" }]);
    expect(queue.current()).toHaveLength(3);
  });

  it("offers an agent in Report a bug's place on a note that owes help once the page can ask one, pending until the ask settles", async () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    queue.show("This window couldn't be closed. Try again.", {
      isLasting: true,
      details: "HTTP 500",
      isHelpOwed: true,
      actions: [{ label: "Try again", run: vi.fn() }],
    });
    queue.show("Docs couldn't open that.", { isLasting: true, details: "HTTP 400" });
    const asked: AgentFixRequest[] = [];
    let settle: () => void = () => undefined;
    let askAgent: ((request: AgentFixRequest) => Promise<unknown>) | null = null;
    const root = mountQueue(
      queue,
      () => () => undefined,
      () => askAgent,
    );
    expect(root.querySelector(".ask-agent-to-fix")).toBeNull();
    expect(root.querySelectorAll(".report-bug")).toHaveLength(1);

    askAgent = (failure) => {
      asked.push(failure);
      return new Promise<void>((resolve) => {
        settle = resolve;
      });
    };
    m.redraw.sync();
    const labels = (): (string | null)[] =>
      Array.from(root.querySelectorAll<HTMLButtonElement>(".toast-action")).map((button) => button.textContent);
    // Only the note that owes help offers an agent, after its own buttons; the report gives way to it.
    expect(labels()).toEqual(["Try again", "Ask an agent to fix this"]);

    const ask = (): HTMLButtonElement => root.querySelector<HTMLButtonElement>(".ask-agent-to-fix")!;
    ask().click();
    m.redraw.sync();
    expect(ask().textContent).toBe("Asking an agent…");
    expect(ask().disabled).toBe(true);
    expect(asked).toEqual([{ whatFailed: "This window couldn't be closed. Try again.", details: "HTTP 500" }]);
    expect(queue.current()).toHaveLength(2);

    settle();
    await Promise.resolve();
    await Promise.resolve();
    m.redraw.sync();
    expect(ask().textContent).toBe("Ask an agent to fix this");
    expect(ask().disabled).toBe(false);
  });

  it("asks the agent what the note says to ask, where that is more than its message and details", () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    const agentRequest: AgentFixRequest = {
      whatFailed: 'Couldn\'t create "notes". This folder is read-only.',
      details: "Tried to create /data/reports/notes/\nHTTP 403",
      instruction: "Do what the user was trying to do if you can.",
    };
    queue.show(agentRequest.whatFailed, { isLasting: true, details: "HTTP 403", isHelpOwed: true, agentRequest });
    const asked: AgentFixRequest[] = [];
    const root = mountQueue(
      queue,
      () => null,
      () => (request) => {
        asked.push(request);
        return Promise.resolve();
      },
    );
    root.querySelector<HTMLButtonElement>(".ask-agent-to-fix")!.click();
    expect(asked).toEqual([agentRequest]);
  });

  it("offers no agent on a note that withholds one, where the ask would go through what failed", () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    queue.show("Chat couldn't send that. Try again.", {
      isLasting: true,
      isHelpOwed: true,
      isAgentWithheld: true,
    });
    const root = mountQueue(
      queue,
      () => () => undefined,
      () => () => Promise.resolve(),
    );
    const labels = Array.from(root.querySelectorAll<HTMLButtonElement>(".toast-action")).map(
      (button) => button.textContent,
    );
    expect(labels).toEqual(["Report a bug"]);
  });

  it("gives a plain note with details a See details and a close control, and holds it up once they are opened", () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    const hold = vi.spyOn(queue, "hold");
    queue.show("Could not close the window.", { isLasting: false, details: "HTTP 409\nWindow win-1 is pinned" });
    queue.show("Could not move the shortcut.");
    const root = mountView(() =>
      m(Toasts, {
        toasts: queue.current(),
        placementClass: "fixed",
        onDismiss: (id) => queue.dismiss(id),
        onHold: (id) => queue.hold(id),
      }),
    );

    const [withDetails, bare] = Array.from(root.querySelectorAll<HTMLElement>("[data-toast]"));
    expect(withDetails.className).toContain("pointer-events-auto");
    expect(withDetails.querySelector(".see-details pre")?.textContent).toBe("HTTP 409\nWindow win-1 is pinned");
    expect(bare.querySelector(".see-details")).toBeNull();
    expect(bare.querySelector("[data-toast-dismiss]")).toBeNull();

    const disclosure = withDetails.querySelector<HTMLDetailsElement>(".see-details")!;
    disclosure.open = true;
    disclosure.dispatchEvent(new Event("toggle"));
    expect(hold).toHaveBeenCalledWith(queue.current()[0].id);
    withDetails.querySelector<HTMLButtonElement>("[data-toast-dismiss]")!.click();
    expect(queue.current().map((toast) => toast.message)).toEqual(["Could not move the shortcut."]);
  });

  it("draws a pending action disabled under its pending label, and as itself once it is no longer pending", () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    let isAsking = false;
    const ask = vi.fn(() => {
      isAsking = true;
    });
    queue.show("Docs is not running right now.", {
      isLasting: true,
      actions: [
        {
          label: "Ask an agent to start it",
          run: ask,
          keepsToast: true,
          pending: { isPending: () => isAsking, label: "Asking an agent…" },
        },
      ],
    });
    const root = mountQueue(queue);
    const button = (): HTMLButtonElement => root.querySelector<HTMLButtonElement>(".toast-action")!;
    expect(button().disabled).toBe(false);

    button().click();
    m.redraw.sync();
    expect(button().textContent).toBe("Asking an agent…");
    expect(button().disabled).toBe(true);
    button().click();
    expect(ask).toHaveBeenCalledTimes(1);

    isAsking = false;
    m.redraw.sync();
    expect(button().textContent).toBe("Ask an agent to start it");
    expect(button().disabled).toBe(false);
  });
});
