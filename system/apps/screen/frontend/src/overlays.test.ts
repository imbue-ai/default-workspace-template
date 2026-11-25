/** The overlays: a failure says what happened in our words, keeps the service's text behind See details and offers
 *  what can help; the agent overlay names a page held for the human and offers to open or drop it (spec section
 *  4.2). */
import { describe, expect, it } from "vitest";

import { readFailure } from "@imbue/workspace-ui/src/models/failure";
import { Overlays, mayCancelPendingUrl } from "./overlays";
import type { AskDelivery, ReportedFailure, StartOutcome, StartOptions } from "@imbue/workspace-ui/src/app_contract";
import type { FailureHost } from "./overlays";
import type { ControlDocument, NekoSessionAnswer, ScreenStateDocument } from "./state";

const OWNER: NekoSessionAnswer = { session_id: "s1", token: "tok-1", name: "Josh", is_owner: true };
const VISITOR: NekoSessionAnswer = { session_id: "s2", token: "tok-2", name: "Mia", is_owner: false };

function state(overrides: Partial<ScreenStateDocument>): ScreenStateDocument {
  return {
    lifecycle: "running",
    install_reason: null,
    install_failure: null,
    stop_reason: null,
    stop_cause: null,
    title: "Screen",
    video_id: "watch",
    control: {
      controller: "agent",
      human_pinned: false,
      human_session_id: null,
      human_name: null,
      agent_id: "chat-a",
      agent_name: "Alice",
      waiting: [],
      idle_seconds: 3,
      idle_release_seconds: 57,
    },
    screen_size: { width: 1280, height: 720, rate: 60 },
    chromium: { state: "running", name: "browser-1", pending_url: null },
    ...overrides,
  };
}

describe("Overlays", () => {
  it("dims the stage while an agent drives and hides the held-page line when none is held", () => {
    const overlays = new Overlays(
      () => undefined,
      () => undefined,
      () => undefined,
      () => undefined,
    );
    overlays.update(state({}), "connected", OWNER, null, null);
    const dim = overlays.dimElement;
    expect(dim.hidden).toBe(false);
    expect(dim.querySelector<HTMLElement>("[data-screen-pending-url]")?.hidden).toBe(true);
    expect(dim.querySelector<HTMLElement>("[data-screen-cancel-pending-url]")?.hidden).toBe(true);
    expect(dim.textContent).toContain("Alice has control");
    expect(dim.textContent).toContain("Take control");
    expect(dim.textContent).not.toContain("open it");
  });

  it("names the held page, relabels Take control, and Cancel asks the service to drop it", () => {
    let cancelled = 0;
    const overlays = new Overlays(
      () => undefined,
      () => undefined,
      () => {
        cancelled += 1;
      },
      () => undefined,
    );
    overlays.update(
      state({ chromium: { state: "running", name: "browser-1", pending_url: "http://localhost:3000/app" } }),
      "connected",
      OWNER,
      null,
      null,
    );
    const dim = overlays.dimElement;
    const pending = dim.querySelector<HTMLElement>("[data-screen-pending-url]");
    expect(pending?.hidden).toBe(false);
    expect(pending?.textContent).toContain("http://localhost:3000/app");
    expect(dim.textContent).toContain("Take control and open it");
    const cancel = dim.querySelector<HTMLButtonElement>("[data-screen-cancel-pending-url]");
    expect(cancel?.hidden).toBe(false);
    cancel?.click();
    expect(cancelled).toBe(1);
  });

  it("names the held page to a visitor who does not control and offers to open it, but not to drop it", () => {
    const overlays = new Overlays(
      () => undefined,
      () => undefined,
      () => undefined,
      () => undefined,
    );
    overlays.update(
      state({ chromium: { state: "running", name: "browser-1", pending_url: "http://localhost:3000/app" } }),
      "connected",
      VISITOR,
      null,
      null,
    );
    const dim = overlays.dimElement;
    expect(dim.querySelector<HTMLElement>("[data-screen-pending-url]")?.hidden).toBe(false);
    expect(dim.textContent).toContain("Take control and open it");
    expect(dim.querySelector<HTMLButtonElement>("[data-screen-cancel-pending-url]")?.hidden).toBe(true);
  });

  it("offers Cancel to the sessions the service lets drop a held page", () => {
    const agentDrives: ControlDocument = state({}).control;
    expect(mayCancelPendingUrl(agentDrives, OWNER)).toBe(true);
    expect(mayCancelPendingUrl(agentDrives, VISITOR)).toBe(false);
    expect(mayCancelPendingUrl(agentDrives, null)).toBe(false);
    const visitorControls: ControlDocument = {
      ...agentDrives,
      controller: "human",
      human_session_id: VISITOR.session_id,
      human_name: VISITOR.name,
      agent_id: null,
      agent_name: null,
    };
    expect(mayCancelPendingUrl(visitorControls, VISITOR)).toBe(true);
    expect(mayCancelPendingUrl(visitorControls, { ...VISITOR, session_id: "s3" })).toBe(false);
  });

  it("lifts the dim once a human holds control, whatever is held", () => {
    const overlays = new Overlays(
      () => undefined,
      () => undefined,
      () => undefined,
      () => undefined,
    );
    const human = state({ chromium: { state: "running", name: "browser-1", pending_url: "http://localhost:1/" } });
    human.control = { ...human.control, controller: "human", human_session_id: "s1", human_name: "Josh" };
    overlays.update(human, "connected", OWNER, null, null);
    expect(overlays.dimElement.hidden).toBe(true);
  });
});

/** A shell connection as the overlay's buttons see it, recording what they asked of it. */
function fakeHost(fields: { hasShell?: boolean; canReportBug?: boolean } = {}): FailureHost & {
  started: string[];
  /** Settles each ask in turn, as the shell's reply would. */
  answers: ((outcome: StartOutcome) => void)[];
  /** Hands each ask an answer that came after it settled as not answered. */
  lateAnswers: ((outcome: StartOutcome) => void)[];
  /** Whether each ask told the shell the page shows its outcome (so the shell toasts nothing). */
  showsOutcome: boolean[];
  /** Where each ask was to go; undefined is the chat about the Screen. */
  deliveries: (AskDelivery | undefined)[];
  reports: ReportedFailure[];
} {
  const host = {
    started: [] as string[],
    answers: [] as ((outcome: StartOutcome) => void)[],
    lateAnswers: [] as ((outcome: StartOutcome) => void)[],
    showsOutcome: [] as boolean[],
    deliveries: [] as (AskDelivery | undefined)[],
    reports: [] as ReportedFailure[],
    hasShell: () => fields.hasShell ?? true,
    canReportBug: () => fields.canReportBug ?? true,
    askAgent: (text: string, options?: StartOptions, delivery?: AskDelivery) => {
      host.started.push(text);
      host.showsOutcome.push(options?.showsOutcome === true);
      host.deliveries.push(delivery);
      host.lateAnswers.push(options?.onLateAnswer ?? (() => undefined));
      return new Promise<StartOutcome>((resolve) => host.answers.push(resolve));
    },
    reportBug: (failure: ReportedFailure) => void host.reports.push(failure),
  };
  return host;
}

function shown(overlays: Overlays): {
  title: string;
  body: string | null;
  details: string | null;
  buttons: string[];
} {
  const element = overlays.element;
  const body = element.querySelector<HTMLElement>("[data-screen-overlay-body]");
  const details = element.querySelector<HTMLElement>("[data-screen-overlay-details]");
  return {
    title: element.querySelector("[data-screen-overlay-title]")?.textContent ?? "",
    body: body?.hidden ? null : (body?.textContent ?? null),
    details: details?.hidden ? null : (details?.querySelector("pre")?.textContent ?? null),
    buttons: Array.from(element.querySelectorAll<HTMLButtonElement>("button"))
      .filter((button) => !button.hidden)
      .map((button) => button.textContent ?? ""),
  };
}

function overlaysWith(
  host: FailureHost | null,
  onStart: () => void = () => undefined,
  onReload: () => void = () => undefined,
): Overlays {
  const overlays = new Overlays(
    onStart,
    () => undefined,
    () => undefined,
    onReload,
  );
  if (host !== null) overlays.setHost(host);
  return overlays;
}

const STOPPED = { lifecycle: "stopped", screen_size: null } as const;

describe("Overlays for the stack's state", () => {
  it("says the Screen is still being set up, with what is missing behind See details and nothing to press", () => {
    const overlays = overlaysWith(fakeHost());
    overlays.update(
      state({ lifecycle: "installing", install_reason: "waiting for env-converge to install neko" }),
      "idle",
      null,
      null,
      null,
    );
    expect(shown(overlays)).toEqual({
      title: "The Screen is still being set up in this workspace.",
      body: "It starts on its own once it's ready; the first time can take a few minutes.",
      details: "waiting for env-converge to install neko",
      buttons: [],
    });
  });

  it("says a failed install could not be set up and asks an agent to fix it with the unit's output", () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    overlays.update(
      state({
        lifecycle: "installing",
        install_reason: "waiting for env-converge to install neko",
        install_failure:
          "env-converge ran system/scripts/env.d/2000-screen.sh in this boot and it failed (exit code 1)",
      }),
      "idle",
      null,
      null,
      null,
    );
    const seen = shown(overlays);
    expect(seen.title).toBe("The Screen couldn't be set up in this workspace.");
    expect(seen.details).toContain("exit code 1");
    expect(seen.buttons).toEqual(["Ask an agent to fix this"]);
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();
    expect(host.started).toHaveLength(1);
    expect(host.started[0]).toContain("The Screen couldn't be set up in this workspace.");
    expect(host.started[0]).toContain("2000-screen.sh in this boot and it failed (exit code 1)");
  });

  it("offers only Start for a stop somebody asked for", () => {
    let starts = 0;
    const overlays = overlaysWith(fakeHost(), () => {
      starts += 1;
    });
    overlays.update(
      state({ ...STOPPED, stop_reason: "Quit from the window menu", stop_cause: "requested" }),
      "idle",
      null,
      null,
      null,
    );
    expect(shown(overlays)).toEqual({
      title: "The Screen was stopped.",
      body: null,
      details: "Quit from the window menu",
      buttons: ["Start"],
    });
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-start]")?.click();
    expect(starts).toBe(1);
  });

  it("says a crash was unexpected and offers Start and an agent, the reason behind See details", () => {
    const overlays = overlaysWith(fakeHost());
    overlays.update(
      state({ ...STOPPED, stop_reason: "screen-neko exited unexpectedly", stop_cause: "crashed" }),
      "idle",
      null,
      null,
      null,
    );
    expect(shown(overlays)).toEqual({
      title: "The Screen stopped unexpectedly.",
      body: null,
      details: "screen-neko exited unexpectedly",
      buttons: ["Start", "Ask an agent to fix this"],
    });
  });

  it("offers a draft of the request and Report a bug under an ask that was refused in a way that owes help", async () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    overlays.update(
      state({ ...STOPPED, stop_reason: "screen-neko exited unexpectedly", stop_cause: "crashed" }),
      "idle",
      null,
      null,
      null,
    );
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();
    host.answers[0]({
      isOpened: false,
      failure: { message: "The workspace couldn't do that. Try again.", details: "boom", isHelpOwed: true },
    });
    await Promise.resolve();
    expect(shown(overlays).buttons).toEqual([
      "Start",
      "Ask an agent to fix this",
      "Draft it in a chat",
      "Report a bug",
    ]);

    overlays.element.querySelector<HTMLButtonElement>("[data-screen-draft-ask]")?.click();
    expect(host.deliveries).toEqual([undefined, "draft"]);
    expect(host.started[1]).toBe(host.started[0]);
    expect(host.showsOutcome).toEqual([true, true]);

    // Another ask takes the refused one's remedies away until it settles.
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();
    expect(shown(overlays).buttons).toEqual(["Start", "Asking an agent…"]);
  });

  it("reads Asking an agent… until the chat opens, and says why when it could not", async () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    const crashed = state({ ...STOPPED, stop_reason: "screen-neko exited unexpectedly", stop_cause: "crashed" });
    overlays.update(crashed, "idle", null, null, null);
    const ask = overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]");
    const askFailure = (): HTMLElement | null =>
      overlays.element.querySelector<HTMLElement>("[data-screen-ask-failure]:not([hidden])");

    ask?.click();
    expect([ask?.textContent, ask?.disabled]).toEqual(["Asking an agent…", true]);
    expect(host.showsOutcome).toEqual([true]);
    // A state that arrives while the shell is opening the chat leaves the press pending.
    overlays.update(crashed, "idle", null, null, null);
    expect([ask?.textContent, ask?.disabled]).toEqual(["Asking an agent…", true]);

    host.answers[0]({
      isOpened: false,
      failure: { message: "Chat couldn't open that.", details: "HTTP 400\nbad launch", isHelpOwed: false },
    });
    await Promise.resolve();
    expect([ask?.textContent, ask?.disabled]).toEqual(["Ask an agent to fix this", false]);
    expect(askFailure()?.querySelector("p")?.textContent).toBe("Chat couldn't open that.");
    expect(askFailure()?.querySelector("pre")?.textContent).toBe("HTTP 400\nbad launch");
    expect(shown(overlays).buttons).toEqual(["Start", "Ask an agent to fix this"]);

    ask?.click();
    expect(askFailure()).toBeNull();
    host.answers[1]({ isOpened: true });
    await Promise.resolve();
    expect([ask?.textContent, ask?.disabled, askFailure()]).toEqual(["Ask an agent to fix this", false, null]);
  });

  it("never reads Asking an agent… on a newer failure's button for an ask made about an older one", async () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    const ask = overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]");
    const askFailure = (): HTMLElement | null =>
      overlays.element.querySelector<HTMLElement>("[data-screen-ask-failure]:not([hidden])");
    overlays.update(
      state({ ...STOPPED, stop_reason: "screen-neko exited unexpectedly", stop_cause: "crashed" }),
      "idle",
      null,
      null,
      null,
    );
    ask?.click();

    overlays.update(
      state({ ...STOPPED, stop_reason: "neko never answered", stop_cause: "start_failed" }),
      "idle",
      null,
      null,
      null,
    );
    expect([ask?.textContent, ask?.disabled]).toEqual(["Ask an agent to fix this", false]);

    // An ask about the newer failure is pending on its own, and the older ask's answer touches neither.
    ask?.click();
    host.answers[0]({
      isOpened: false,
      failure: { message: "Chat couldn't open that.", details: null, isHelpOwed: false },
    });
    await Promise.resolve();
    expect([ask?.textContent, ask?.disabled, askFailure()]).toEqual(["Asking an agent…", true, null]);
    host.answers[1]({ isOpened: true });
    await Promise.resolve();
    expect([ask?.textContent, ask?.disabled, askFailure()]).toEqual(["Ask an agent to fix this", false, null]);
  });

  it("replaces a not-answered ask with the shell's late answer, and files both failures through one Report a bug", async () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    const askFailure = (): HTMLElement | null =>
      overlays.element.querySelector<HTMLElement>("[data-screen-ask-failure]:not([hidden])");
    const reportButtons = (): HTMLButtonElement[] =>
      Array.from(overlays.element.querySelectorAll<HTMLButtonElement>("button")).filter(
        (button) => !button.hidden && button.textContent === "Report a bug",
      );
    overlays.update(
      state({ ...STOPPED, stop_reason: "screen-neko exited unexpectedly", stop_cause: "crashed" }),
      "idle",
      null,
      null,
      null,
    );
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();
    host.answers[0]({
      isOpened: false,
      failure: { message: "The workspace didn't answer.", details: null, isHelpOwed: false },
    });
    await Promise.resolve();
    expect(askFailure()?.querySelector("p")?.textContent).toBe("The workspace didn't answer.");

    const late = { message: "The workspace couldn't do that.", details: "boom", isHelpOwed: true };
    host.lateAnswers[0]({ isOpened: false, failure: late });
    expect(askFailure()?.querySelector("p")?.textContent).toBe(late.message);
    expect(reportButtons()).toHaveLength(1);
    reportButtons()[0].click();
    expect(host.reports).toEqual([
      {
        message: "The Screen stopped unexpectedly.",
        details:
          "screen-neko exited unexpectedly\n\nAsking an agent to fix this failed too:\nThe workspace couldn't do that.\nboom",
      },
    ]);

    host.lateAnswers[0]({ isOpened: true });
    expect(askFailure()).toBeNull();
  });

  it("offers Report a bug for a refused ask that owes help, on a failure that would offer none without an agent", async () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    overlays.update(
      state({ ...STOPPED, stop_reason: "screen-neko exited unexpectedly", stop_cause: "out_of_memory" }),
      "idle",
      null,
      null,
      null,
    );
    const report = overlays.element.querySelector<HTMLButtonElement>("[data-screen-report-bug]");
    expect(report?.hidden).toBe(true);
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();
    host.answers[0]({
      isOpened: false,
      failure: { message: "The workspace couldn't do that.", details: "boom", isHelpOwed: true },
    });
    await Promise.resolve();
    expect(report?.hidden).toBe(false);
  });

  it("drops a failed ask once the Screen runs again, and one that settles after it did", async () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    const crashed = state({ ...STOPPED, stop_reason: "screen-neko exited unexpectedly", stop_cause: "crashed" });
    const notOpened: StartOutcome = {
      isOpened: false,
      failure: { message: "Chat couldn't open that.", details: null, isHelpOwed: false },
    };
    const askFailure = (): HTMLElement | null =>
      overlays.element.querySelector<HTMLElement>("[data-screen-ask-failure]:not([hidden])");
    const ask = (): void => overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();

    overlays.update(crashed, "idle", null, null, null);
    ask();
    host.answers[0](notOpened);
    await Promise.resolve();
    expect(askFailure()).not.toBeNull();
    overlays.update(state({}), "connected", OWNER, null, null);
    overlays.update(crashed, "idle", null, null, null);
    expect(askFailure()).toBeNull();

    ask();
    overlays.update(state({}), "connected", OWNER, null, null);
    host.answers[1](notOpened);
    await Promise.resolve();
    overlays.update(crashed, "idle", null, null, null);
    expect(askFailure()).toBeNull();
  });

  it("names low memory for a member the memory guard shed, and offers an agent", () => {
    const overlays = overlaysWith(fakeHost());
    overlays.update(
      state({ ...STOPPED, stop_reason: "screen-neko exited unexpectedly", stop_cause: "out_of_memory" }),
      "idle",
      null,
      null,
      null,
    );
    const seen = shown(overlays);
    expect(seen.title).toBe("The Screen stopped unexpectedly.");
    expect(seen.body).toContain("ran low on memory");
    expect(seen.buttons).toEqual(["Start", "Ask an agent to fix this"]);
  });

  it("says a start that failed with Try again and an agent", () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    overlays.update(
      state({ ...STOPPED, stop_reason: "the stack did not start: neko never answered", stop_cause: "start_failed" }),
      "idle",
      null,
      null,
      null,
    );
    expect(shown(overlays)).toEqual({
      title: "The Screen couldn't start. Try again.",
      body: null,
      details: "the stack did not start: neko never answered",
      buttons: ["Try again", "Ask an agent to fix this"],
    });
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();
    expect(host.started[0]).toContain("The Screen couldn't start.");
    expect(host.started[0]).toContain("neko never answered");
  });

  it("says a Start refused again after a failed start once, not twice", () => {
    const overlays = overlaysWith(fakeHost());
    overlays.update(
      state({ ...STOPPED, stop_reason: "neko never answered", stop_cause: "start_failed" }),
      "idle",
      null,
      null,
      {
        message: "The Screen couldn't start. Try again.",
        details: "HTTP 500\nsupervisord refused the start",
        isHelpOwed: true,
      },
    );
    expect(shown(overlays)).toMatchObject({
      title: "The Screen couldn't start. Try again.",
      body: null,
      details: "HTTP 500\nsupervisord refused the start\nneko never answered",
    });
  });

  it("offers Report a bug in the agent's place only where no agent can be asked and the failure owes help", () => {
    const crashed = state({ ...STOPPED, stop_reason: "screen-xorg exited unexpectedly", stop_cause: "crashed" });
    const shed = state({ ...STOPPED, stop_reason: "screen-xorg exited unexpectedly", stop_cause: "out_of_memory" });
    const host = fakeHost({ hasShell: false });
    const noAgent = overlaysWith(host);
    noAgent.update(crashed, "idle", null, null, null);
    expect(shown(noAgent).buttons).toEqual(["Start", "Report a bug"]);
    noAgent.element.querySelector<HTMLButtonElement>("[data-screen-report-bug]")?.click();
    expect(host.reports).toEqual([
      { message: "The Screen stopped unexpectedly.", details: "screen-xorg exited unexpectedly" },
    ]);
    noAgent.update(shed, "idle", null, null, null);
    expect(shown(noAgent).buttons).toEqual(["Start"]);
    const neither = overlaysWith(fakeHost({ hasShell: false, canReportBug: false }));
    neither.update(crashed, "idle", null, null, null);
    expect(shown(neither).buttons).toEqual(["Start"]);
    const unframed = overlaysWith(null);
    unframed.update(crashed, "idle", null, null, null);
    expect(shown(unframed).buttons).toEqual(["Start"]);
  });

  it("says a refused Start under the title, its details first", () => {
    const overlays = overlaysWith(fakeHost());
    overlays.update(
      state({ ...STOPPED, stop_reason: "Quit from the window menu", stop_cause: "requested" }),
      "idle",
      null,
      null,
      {
        message: "The workspace didn't answer. This may not have gone through.",
        details: null,
        isHelpOwed: false,
      },
    );
    expect(shown(overlays)).toEqual({
      title: "The Screen was stopped.",
      body: "The workspace didn't answer. This may not have gone through.",
      details: "Quit from the window menu",
      buttons: ["Start"],
    });
  });

  it("offers an agent for a refused Start that owes help, over a stop that owes none, and files both once the ask is refused", async () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    overlays.update(
      state({ ...STOPPED, stop_reason: "Quit from the window menu", stop_cause: "requested" }),
      "idle",
      null,
      null,
      {
        message: "The Screen couldn't start. Try again.",
        details: "HTTP 500\nsupervisord refused the start",
        isHelpOwed: true,
      },
    );
    expect(shown(overlays).buttons).toEqual(["Start", "Ask an agent to fix this"]);
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();
    expect(host.started).toHaveLength(1);
    expect(host.started[0]).toMatch(/^Imbue Studio showed the user this failure: The Screen couldn't start\.\n\n/);
    expect(host.started[0]).toContain("HTTP 500\nsupervisord refused the start\nQuit from the window menu");
    host.answers[0]({
      isOpened: false,
      failure: { message: "Chat couldn't open that. Try again.", details: null, isHelpOwed: true },
    });
    await Promise.resolve();
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-report-bug]")?.click();
    expect(host.reports).toEqual([
      {
        message: "The Screen was stopped. The Screen couldn't start. Try again.",
        details:
          "HTTP 500\nsupervisord refused the start\nQuit from the window menu\n\n" +
          "Asking an agent to fix this failed too:\nChat couldn't open that. Try again.",
      },
    ]);
  });

  it("keeps a refused sign-in's reason behind See details while it retries", () => {
    const overlays = overlaysWith(fakeHost());
    overlays.update(
      state({ control: { ...state({}).control, controller: "nobody" } }),
      "idle",
      null,
      "HTTP 502\nneko refused the login: bad password",
      null,
    );
    expect(shown(overlays)).toEqual({
      title: "Couldn't connect to the Screen. Retrying…",
      body: null,
      details: "HTTP 502\nneko refused the login: bad password",
      buttons: [],
    });
  });

  it("names the Screen when its state got no answer, and Try again loads it again", () => {
    let reloads = 0;
    const overlays = overlaysWith(
      fakeHost(),
      () => undefined,
      () => (reloads += 1),
    );
    expect(shown(overlays).title).toBe("Loading the Screen");
    overlays.showLoadFailure(readFailure({ status: 0, body: null, rawText: "Failed to fetch", requestKind: "read" }));
    expect(shown(overlays)).toEqual({
      title: "The Screen couldn't load.",
      body: "The workspace didn't answer. Try again in a moment.",
      details: "Failed to fetch",
      buttons: ["Try again"],
    });
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-reload]")?.click();
    expect(reloads).toBe(1);
  });

  it("offers Try again and an agent when the service failed to answer its state for no reason it gave", () => {
    const host = fakeHost();
    const overlays = overlaysWith(host);
    overlays.showLoadFailure(
      readFailure({ status: 500, body: null, rawText: "Internal Server Error", requestKind: "read" }),
    );
    expect(shown(overlays)).toEqual({
      title: "The Screen couldn't load. Try again.",
      body: null,
      details: "HTTP 500\nInternal Server Error",
      buttons: ["Try again", "Ask an agent to fix this"],
    });
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();
    expect(host.started[0]).toMatch(/^Imbue Studio showed the user this failure: The Screen couldn't load\.\n\n/);
    expect(host.started[0]).toContain("HTTP 500\nInternal Server Error");
    expect(host.showsOutcome).toEqual([true]);
  });

  it("offers Report a bug under a refused ask once a handshake that came after it says one opens", async () => {
    const canReportBug = { value: false };
    const asked = fakeHost();
    const host = { ...asked, canReportBug: () => canReportBug.value };
    const overlays = overlaysWith(host);
    overlays.showLoadFailure(
      readFailure({ status: 500, body: null, rawText: "Internal Server Error", requestKind: "read" }),
    );
    overlays.element.querySelector<HTMLButtonElement>("[data-screen-ask-agent]")?.click();
    asked.answers[0]({
      isOpened: false,
      failure: { message: "The workspace couldn't do that. Try again.", details: null, isHelpOwed: true },
    });
    await Promise.resolve();
    expect(shown(overlays).buttons).toEqual(["Try again", "Ask an agent to fix this", "Draft it in a chat"]);

    canReportBug.value = true;
    overlays.syncShellButtons();

    expect(shown(overlays).buttons).toEqual([
      "Try again",
      "Ask an agent to fix this",
      "Draft it in a chat",
      "Report a bug",
    ]);
  });

  it("shows no agent dim before any state says an agent drives", () => {
    const overlays = overlaysWith(fakeHost());
    expect(overlays.dimElement.hidden).toBe(true);
  });
});
