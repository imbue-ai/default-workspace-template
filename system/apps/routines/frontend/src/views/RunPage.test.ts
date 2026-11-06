// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";

import { runDetailOf, runOf } from "../testing/records";
import { RunPage } from "./RunPage";

afterEach(() => unmountViews());

describe("RunPage", () => {
  it("heads the run with its outcome and shows its log as text", () => {
    const root = mountView(() =>
      m(RunPage, { detail: runDetailOf({ log: "<b>not markup</b>\ndone\n" }), routineTitle: "News sync" }),
    );
    expect(root.querySelector("h1")?.textContent).toBe("Worked, 10 minutes ago");
    expect(root.textContent).toContain("Started on its schedule, took 12s, exit code 0");
    expect(root.querySelector("[data-summary]")?.textContent).toBe("3 headlines pulled");
    const log = root.querySelector("[data-log]")!;
    expect(log.textContent).toBe("<b>not markup</b>\ndone\n");
    expect(log.querySelector("b")).toBeNull();
    expect([...root.querySelectorAll("a")].map((link) => link.getAttribute("href"))).toEqual([
      "/",
      "/news-sync",
      "/news-sync/runs/run-1/log",
    ]);
  });

  it("keeps the end of a growing log in view, unless the reader scrolled up", () => {
    let log = "line 1\n";
    const root = mountView(() => m(RunPage, { detail: runDetailOf({ log }), routineTitle: "News sync" }));
    const pre = root.querySelector<HTMLPreElement>("[data-log]")!;
    // jsdom lays nothing out, so the log's geometry is stubbed: 500px of text in a 100px box.
    let scrollTop = 0;
    Object.defineProperty(pre, "scrollHeight", { get: () => 500 });
    Object.defineProperty(pre, "clientHeight", { get: () => 100 });
    Object.defineProperty(pre, "scrollTop", { get: () => scrollTop, set: (value: number) => (scrollTop = value) });

    scrollTop = 400;
    log = "line 1\nline 2\n";
    m.redraw.sync();
    expect(scrollTop).toBe(500);

    scrollTop = 120;
    log = "line 1\nline 2\nline 3\n";
    m.redraw.sync();
    expect(scrollTop).toBe(120);
  });

  it("links the chat and the result's links, and says when only the end of the log is shown", () => {
    const root = mountView(() =>
      m(RunPage, {
        detail: runDetailOf({
          run: runOf({ kind: "SKILL", chat_id: "agent-1", warnings: ["RESTARTED_PREVIOUS_RUN"], exit_code: null }),
          log: "",
          is_log_truncated: true,
          links: [{ label: "The digest", url: "http://localhost:3000/digest" }],
        }),
        routineTitle: "Digest",
      }),
    );
    const links = [...root.querySelectorAll("a")].map((link) => [link.textContent, link.getAttribute("href")]);
    expect(links).toContainEqual(["The digest", "http://localhost:3000/digest"]);
    expect(links.find(([text]) => text === "Open its chat")?.[1]).toContain("?chat=agent-1");
    expect(root.textContent).toContain("The previous run was still going and was cut off so this one could start.");
    expect(root.textContent).toContain("Only the end is shown here.");
    expect(root.querySelector("[data-log]")?.textContent).toBe("Nothing was printed.");
    expect(root.textContent).not.toContain("exit code");
  });
});
